"""LLM-judge 评估引擎：对异常 trace 做质量评估 + 提炼候选规则。

流程：取 trace 摘要 + 末步 output → 构造 prompt（内置 rubric）→ 调 LLM
→ 解析 JSON → 写 trace_scores → 若有可量化异常则生成候选规则(pending)。

触发：只评异常 trace（命中规则标红 或 status=failed）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import app.core.db as db
from app.core import llm
from app.diagnosis.rubric import get_rubric

logger = logging.getLogger("evolution.judge")

# 末步 output 截断长度（控制 LLM token 成本）
_OUTPUT_TRUNCATE = 2000


def is_anomalous(trace_id: str) -> bool:
    """判断 trace 是否异常（需 LLM-judge 评估）。

    异常定义：status=failed，或命中了任何规则标红。
    """
    run = db.query_one("SELECT status FROM runs WHERE trace_id = ?", (trace_id,))
    if run is None:
        return False
    if run["status"] == "failed":
        return True
    flags = db.query_one("SELECT count(*) AS c FROM trace_flags WHERE trace_id = ?", (trace_id,))
    return (flags["c"] if flags else 0) > 0


def judge_trace(trace_id: str) -> dict[str, Any] | None:
    """对一个 trace 跑完整 LLM-judge 评估。

    Returns: 评估结果摘要 dict（含 score/verdict/rule_count），失败返回 None。
    幂等：judgment_runs 有 done 记录则跳过。
    """
    if not llm.judge_enabled():
        logger.warning("judge_trace 跳过：LLM 未配置")
        return None

    # 幂等：已评估过则跳过
    existing = db.query_one("SELECT status FROM judgment_runs WHERE trace_id = ?", (trace_id,))
    if existing and existing["status"] == "done":
        return None

    # 占位/标记进行中
    now = datetime.now(UTC).isoformat()
    db.execute(
        """INSERT INTO judgment_runs (trace_id, status, started_at) VALUES (?, 'pending', ?)
           ON CONFLICT(trace_id) DO UPDATE SET status='pending', started_at=excluded.started_at, error=NULL""",
        (trace_id, now),
    )

    try:
        brief = _build_trace_brief(trace_id)
        prompt_messages = _build_prompt(brief)
        raw = llm.chat(prompt_messages)
        result = _parse_response(raw)
        _save_score(trace_id, result)
        rule_count = _save_rule_suggestions(trace_id, result.get("rule_suggestions", []))
        db.execute(
            "UPDATE judgment_runs SET status='done', finished_at=? WHERE trace_id=?",
            (datetime.now(UTC).isoformat(), trace_id),
        )
        return {
            "score": result.get("overall"),
            "verdict": result.get("verdict"),
            "rule_count": rule_count,
        }
    except Exception as exc:
        logger.exception("judge_trace 失败 %s", trace_id)
        db.execute(
            "UPDATE judgment_runs SET status='error', error=?, finished_at=? WHERE trace_id=?",
            (str(exc)[:500], datetime.now(UTC).isoformat(), trace_id),
        )
        return None


def _build_trace_brief(trace_id: str) -> dict[str, Any]:
    """构造给 LLM 的 trace 摘要：结构化数据 + 末步 output 截断。"""
    run = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run is None:
        raise ValueError(f"trace 不存在: {trace_id}")

    # 结构化摘要：agent 序列、耗时分布、错误、token
    agents = db.query_all(
        """SELECT agent_name, count(*) AS n, sum(duration_ms) AS total_ms,
                  sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
           FROM nodes WHERE trace_id=? AND kind='agent' GROUP BY agent_name""",
        (trace_id,),
    )
    agg = db.query_one(
        """SELECT
            (SELECT count(*) FROM nodes WHERE trace_id=? AND kind='llm') AS llm_calls,
            (SELECT count(*) FROM nodes WHERE trace_id=? AND kind='tool') AS tool_calls,
            (SELECT count(*) FROM nodes WHERE trace_id=? AND kind='error') AS errors,
            (SELECT COALESCE(sum(usage_total),0) FROM nodes WHERE trace_id=? AND kind='llm') AS tokens
        """,
        (trace_id, trace_id, trace_id, trace_id),
    )

    # 末步 output：取最后一条 chain_summary 非空的节点，从 event_payloads 取正文
    last_output = _last_node_output_text(trace_id)

    return {
        "trace_id": trace_id,
        "endpoint": run["endpoint"],
        "status": run["status"],
        "duration_ms": run["duration_ms"],
        "event_count": run["event_count"],
        "error": run["error"],
        "agents": [dict(a) for a in agents],
        "llm_calls": agg["llm_calls"] if agg else 0,
        "tool_calls": agg["tool_calls"] if agg else 0,
        "error_count": agg["errors"] if agg else 0,
        "total_tokens": agg["tokens"] if agg else 0,
        "last_output": last_output,
    }


def _last_node_output_text(trace_id: str) -> str:
    """取 trace 最后一个有正文输出的节点文本，截断到 _OUTPUT_TRUNCATE 字符。"""
    # 从 event_payloads 找最后一条 tool_end / llm_end 事件的 output
    rows = db.query_all(
        """SELECT payload_json FROM event_payloads
           WHERE trace_id=? AND type IN ('tool_end','llm_end') ORDER BY sequence DESC LIMIT 20""",
        (trace_id,),
    )
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except json.JSONDecodeError:
            continue
        text = _extract_text(payload.get("output")) or _extract_text(payload.get("tool_output"))
        if text and text.strip():
            return text[:_OUTPUT_TRUNCATE]
    return "(无可用产出文本)"


def _extract_text(value: Any) -> str | None:
    """从 output 结构里提取纯文本（兼容多种格式）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, list):
            for msg in reversed(messages):
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    parts = [b.get("text", "") for b in content if isinstance(b, dict)]
                    joined = "\n".join(p for p in parts if p)
                    if joined.strip():
                        return joined
        content = value.get("content")
        if isinstance(content, str):
            return content
    return None


def _build_prompt(brief: dict[str, Any]) -> list[dict[str, str]]:
    """构造 LLM prompt（rubric + trace 摘要）。"""
    agent_lines = "\n".join(
        f"  - {a['agent_name']}: 调用 {a['n']} 次, 失败 {a['failed'] or 0}, 总耗时 {a['total_ms'] or 0}ms"
        for a in brief["agents"]
    ) or "  (无 agent 记录)"

    brief_text = f"""## 待评估 trace
- endpoint: {brief['endpoint']}
- 状态: {brief['status']}
- 总耗时: {brief['duration_ms']}ms
- 事件数: {brief['event_count']}
- LLM 调用: {brief['llm_calls']}, Tool 调用: {brief['tool_calls']}, error 节点: {brief['error_count']}
- Token 消耗: {brief['total_tokens']}
- 运行时错误: {brief['error'] or '(无)'}

## Agent 编排
{agent_lines}

## 最终产出（末步 output 截断）
{brief['last_output']}
"""
    return [
        {"role": "system", "content": get_rubric()},
        {"role": "user", "content": brief_text},
    ]


def _parse_response(raw: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON（容错：剥离 markdown 代码块、提取首个 JSON 对象）。"""
    text = raw.strip()
    # 剥离 ```json ... ``` 代码块
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 兜底：提取首个 {...} 块
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"无法解析 LLM 返回为 JSON: {raw[:200]}")


def _save_score(trace_id: str, result: dict[str, Any]) -> None:
    """写 trace_scores。"""
    db.execute(
        """INSERT INTO trace_scores (trace_id, score, verdict, rubric_json, summary, scored_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            trace_id,
            float(result.get("overall", 0)),
            result.get("verdict", "review"),
            json.dumps(result.get("scores", {}), ensure_ascii=False),
            result.get("summary", ""),
            datetime.now(UTC).isoformat(),
        ),
    )


# 合法 metric 白名单（防 LLM 乱填）
_VALID_METRICS = {"duration_ms", "total_tokens", "error_count", "event_count", "status"}
_VALID_OPS = {">", ">=", "<", "<=", "==", "!="}


def _save_rule_suggestions(trace_id: str, suggestions: list[dict[str, Any]]) -> int:
    """把 LLM 的规则建议写成候选规则（status=pending）。返回写入数。"""
    if not suggestions:
        return 0
    now = datetime.now(UTC).isoformat()
    count = 0
    for sug in suggestions:
        metric = str(sug.get("metric", "")).strip()
        op = str(sug.get("op", "")).strip()
        threshold = str(sug.get("threshold", "")).strip()
        if metric not in _VALID_METRICS or op not in _VALID_OPS or not threshold:
            continue  # 过滤非法值
        reason = str(sug.get("reason", ""))[:300]
        # 去重：同样的 metric/op/threshold 已有候选则不重复加
        dup = db.query_one(
            """SELECT id FROM rules WHERE metric=? AND op=? AND threshold=? AND source='llm_candidate'""",
            (metric, op, threshold),
        )
        if dup:
            continue
        db.execute(
            """INSERT INTO rules (name, metric, op, threshold, enabled, source, created_at, description, status, confidence, evidence, source_trace_id)
               VALUES (?, ?, ?, ?, 0, 'llm_candidate', ?, ?, 'pending', ?, ?, ?)""",
            (
                f"LLM建议: {metric} {op} {threshold}", metric, op, threshold,
                now, reason, None, reason, trace_id,
            ),
        )
        count += 1
    return count
