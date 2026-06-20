"""双层评估引擎（T1.3，决策 D2/D3/D4）。

编排（5 次 judge 调用，异源模型）：
  - 内容维度 1 次：取 writing 正文 → 6 内容指标打分
  - subagent 维度 4 次：各取该 subagent 文件交付物 → 各能力维度打分

每次调用：rubric prompt + 交付物文本 → 异源 LLM → 解析 JSON 分数。
幂等：evaluation_runs 有 done 记录则跳过（同 trace 不重评）。

设计依据：设计文档 D2(5次编排)/D3(异源)/D4(取文件交付物)/D11(粗标三档)。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import app.db as db
from app import eval_extractor, llm
from app.rubrics import xianxia as rubric

logger = logging.getLogger("monitoring.evaluation")


def evaluate_trace(trace_id: str) -> dict[str, Any] | None:
    """对一个 trace 跑完整双层评估。

    Returns: 评估结果摘要 dict（含各层分数/badcase 标记），失败/跳过返回 None。
    幂等：evaluation_runs 有 done 记录则跳过。
    """
    if not llm.judge_enabled():
        logger.warning("evaluate_trace 跳过：LLM 未配置")
        return None

    # 幂等：已评估过则跳过
    existing = db.query_one("SELECT status FROM evaluation_runs WHERE trace_id = ?", (trace_id,))
    if existing and existing["status"] == "done":
        return None

    # 标记进行中
    now = datetime.now(UTC).isoformat()
    db.execute(
        """INSERT INTO evaluation_runs (trace_id, status, started_at) VALUES (?, 'pending', ?)
           ON CONFLICT(trace_id) DO UPDATE SET status='pending', started_at=excluded.started_at, error=NULL""",
        (trace_id, now),
    )

    try:
        # 1. 提取各 subagent 交付物
        deliveries = eval_extractor.extract_deliveries(trace_id)

        # 2. 内容维度评估（writing 正文）
        content_result = _evaluate_content_layer(trace_id, deliveries)

        # 3. subagent 维度评估（4 个 subagent 各一次）
        subagent_results = _evaluate_subagent_layer(trace_id, deliveries)

        # 4. 落盘
        _save_scores(trace_id, content_result, subagent_results)

        # 5. badcase 判定（任一维度低即 badcase，D 决策）
        badcase = _detect_badcase(content_result, subagent_results)

        db.execute(
            "UPDATE evaluation_runs SET status='done', finished_at=? WHERE trace_id=?",
            (datetime.now(UTC).isoformat(), trace_id),
        )

        # 6. 自动连锁（Phase 2）：badcase → 诊断 → 生成候选 prompt
        # 失败不阻塞评估主流程（候选可经 API 手动重试）
        candidates: list[dict[str, Any]] = []
        if badcase["is_badcase"]:
            try:
                from app.diagnosis import diagnose_badcase
                from app.optimizer import optimize_candidate
                diagnosed = diagnose_badcase(trace_id, badcase)
                for cand in diagnosed:
                    if "id" in cand:
                        optimized = optimize_candidate(cand["id"])
                        if optimized:
                            cand["optimized"] = optimized
                        candidates.append(cand)
            except Exception:
                logger.exception("自动连锁诊断/优化失败 %s", trace_id)

        return {
            "content": content_result,
            "subagent": subagent_results,
            "badcase": badcase,
            "candidates": candidates,
        }
    except Exception as exc:
        logger.exception("evaluate_trace 失败 %s", trace_id)
        db.execute(
            "UPDATE evaluation_runs SET status='error', error=?, finished_at=? WHERE trace_id=?",
            (str(exc)[:500], datetime.now(UTC).isoformat(), trace_id),
        )
        return None


def _evaluate_content_layer(
    trace_id: str, deliveries: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """内容维度：取 writing 正文 → 6 内容指标打分。"""
    content_text = eval_extractor.get_content_layer_text(trace_id)
    if not content_text:
        logger.warning("内容维度评估跳过 %s：无 writing 交付物", trace_id)
        return {"skipped": True, "reason": "无 writing 正文交付物"}

    rubric_prompt = rubric.build_content_rubric_prompt()
    output_format = rubric.build_output_format(rubric.content_dim_keys())
    messages = [
        {"role": "system", "content": rubric_prompt + output_format},
        {"role": "user", "content": f"## 待评估作品正文\n\n{content_text}"},
    ]
    raw = llm.chat(messages)
    result = _parse_response(raw)
    return {
        "skipped": False,
        "scores": result.get("scores", {}),
        "overall": result.get("overall", 0),
        "verdict": result.get("verdict", "review"),
        "evidence": result.get("evidence", ""),
    }


def _evaluate_subagent_layer(
    trace_id: str, deliveries: dict[str, dict[str, str]]
) -> dict[str, dict[str, Any]]:
    """subagent 维度：4 个 subagent 各评一次。"""
    results: dict[str, dict[str, Any]] = {}
    for dim in rubric.SUBAGENT_DIMENSIONS:
        agent = dim["agent"]
        agent_text = eval_extractor.get_agent_delivery_text(trace_id, agent)
        if not agent_text:
            results[agent] = {"skipped": True, "reason": f"无 {agent} 交付物"}
            continue

        rubric_prompt = rubric.build_subagent_rubric_prompt(agent)
        output_format = rubric.build_output_format([dim["key"]])
        messages = [
            {"role": "system", "content": rubric_prompt + output_format},
            {"role": "user", "content": f"## {agent} 环节交付物\n\n{agent_text}"},
        ]
        raw = llm.chat(messages)
        result = _parse_response(raw)
        scores = result.get("scores", {})
        # subagent 单维度，取该维度分
        score = scores.get(dim["key"], result.get("overall", 0))
        results[agent] = {
            "skipped": False,
            "key": dim["key"],
            "score": score,
            "verdict": result.get("verdict", "review"),
            "evidence": result.get("evidence", ""),
        }
    return results


def _parse_response(raw: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON（容错：剥离 markdown 代码块、提取首个 JSON 对象）。"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"无法解析 LLM 返回为 JSON: {raw[:200]}")


def _save_scores(
    trace_id: str,
    content_result: dict[str, Any],
    subagent_results: dict[str, dict[str, Any]],
) -> None:
    """写 evaluation_scores 表。"""
    now = datetime.now(UTC).isoformat()
    rows: list[tuple[Any, ...]] = []

    # 内容维度
    if not content_result.get("skipped"):
        for metric, score in content_result.get("scores", {}).items():
            rows.append((
                trace_id, "content", "novel", metric,
                float(score), content_result.get("evidence", ""), now,
            ))

    # subagent 维度
    for agent, res in subagent_results.items():
        if res.get("skipped"):
            continue
        rows.append((
            trace_id, "subagent", agent, res.get("key", agent),
            float(res.get("score", 0)), res.get("evidence", ""), now,
        ))

    if rows:
        db.executemany(
            """INSERT INTO evaluation_scores
               (trace_id, layer, target, metric, score, evidence, scored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def _detect_badcase(
    content_result: dict[str, Any],
    subagent_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """badcase 判定：任一维度低于阈值即 badcase（需求决策）。"""
    flagged: list[dict[str, Any]] = []

    # 内容维度
    if not content_result.get("skipped"):
        threshold = rubric.CONTENT_BADCASE_THRESHOLD
        for metric, score in content_result.get("scores", {}).items():
            if float(score) < threshold:
                flagged.append({
                    "layer": "content", "target": "novel",
                    "metric": metric, "score": float(score), "threshold": threshold,
                })

    # subagent 维度
    threshold = rubric.SUBAGENT_BADCASE_THRESHOLD
    for agent, res in subagent_results.items():
        if res.get("skipped"):
            continue
        if float(res.get("score", 0)) < threshold:
            flagged.append({
                "layer": "subagent", "target": agent,
                "metric": res.get("key", agent),
                "score": float(res.get("score", 0)), "threshold": threshold,
            })

    return {
        "is_badcase": len(flagged) > 0,
        "flagged_dimensions": flagged,
    }
