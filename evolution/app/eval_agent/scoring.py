"""28 维五级锚点评估打分（第三期，2026-07）。

编排（按任务契约自适应启用评分组，每组 1 次 LLM 调用）：
  - 通用五维（所有任务适用）
  - 玄幻故事构建六维（有 storybuilding 交付物时）
  - 玄幻详细大纲六维（有 detail-outline 交付物时）
  - 玄幻正文十一维（有 writing 交付物时）

每次调用：rubric prompt + 交付物文本 → LLM → 解析 JSON 五级锚定分（1-5）。
幂等：evaluation_runs 有 done 记录则跳过（同 trace 不重评）。

设计依据：.claude/md/20260726_214821_trace证据管线.md § 评估维度矩阵
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import app.core.db as db
from app.eval_agent import eval_extractor
from app.core import llm
from app.eval_agent.rubrics import xianxia as rubric

logger = logging.getLogger("evolution.eval_agent.scoring")


def evaluate_trace(trace_id: str) -> dict[str, Any] | None:
    """对一个 trace 跑完整 28 维评估。

    Returns: 评估结果 dict（含各组分数/badcase 标记/校准状态），失败/跳过返回 None。
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
        # 1. 提取交付物 + 判断启用哪些评分组
        deliveries = eval_extractor.extract_deliveries(trace_id)
        groups = rubric.applicable_groups(deliveries)

        # 2. 按组评估（每组 1 次 LLM 调用）
        group_results: dict[str, dict[str, Any]] = {}
        for group_name, dimensions in groups.items():
            result = _evaluate_group(trace_id, group_name, dimensions, deliveries)
            group_results[group_name] = result

        # 3. 落盘
        _save_scores(trace_id, group_results)

        # 4. badcase 判定
        badcase = _detect_badcase(group_results)

        db.execute(
            "UPDATE evaluation_runs SET status='done', finished_at=? WHERE trace_id=?",
            (datetime.now(UTC).isoformat(), trace_id),
        )

        # 5. 反思归纳（badcase 时自动归纳，异常不阻断）
        if badcase.get("is_badcase"):
            try:
                from app.reflection.extractor import extract_from_eval
                extract_from_eval(trace_id, {"badcase": badcase})
            except Exception:
                logger.warning("反思归纳异常（不阻断评估）trace=%s", trace_id, exc_info=True)

        return {
            "groups": group_results,
            "badcase": badcase,
            "calibration": rubric.CALIBRATION_STATUS,
            "rubric_version": rubric.RUBRIC_VERSION,
        }
    except Exception as exc:
        logger.exception("evaluate_trace 失败 %s", trace_id)
        db.execute(
            "UPDATE evaluation_runs SET status='error', error=?, finished_at=? WHERE trace_id=?",
            (str(exc)[:500], datetime.now(UTC).isoformat(), trace_id),
        )
        return None


def _evaluate_group(
    trace_id: str,
    group_name: str,
    dimensions: list[dict],
    deliveries: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """评估单个评分组（1 次 LLM 调用）。

    通用五维基于全部交付物拼接文本；其他组基于对应 agent 的交付物。
    """
    artifact_text = _get_group_artifact_text(group_name, deliveries)
    if not artifact_text:
        label = rubric.GROUP_LABELS.get(group_name, group_name)
        return {"skipped": True, "reason": f"无 {label} 交付物"}

    dim_keys = [d["key"] for d in dimensions]
    rubric_prompt = rubric.build_group_rubric_prompt(group_name, dimensions)
    output_format = rubric.build_output_format(dim_keys)

    messages = [
        {"role": "system", "content": rubric_prompt + output_format},
        {"role": "user", "content": f"## 待评估产物\n\n{artifact_text}"},
    ]
    raw = llm.chat(messages)
    result = _parse_response(raw)

    scores = result.get("scores", {})
    # 过滤 score=0（无法判断）的维度，不计入组分
    valid_scores = {k: v for k, v in scores.items() if isinstance(v, (int, float)) and v > 0}
    group_score = (
        round(sum(valid_scores.values()) / len(valid_scores), 2)
        if valid_scores else 0
    )

    return {
        "skipped": False,
        "scores": scores,
        "group_score": group_score,
        "valid_dim_count": len(valid_scores),
        "total_dim_count": len(dim_keys),
        "evidence": result.get("evidence", ""),
        "verdict": result.get("verdict", "review"),
    }


def _get_group_artifact_text(
    group_name: str,
    deliveries: dict[str, dict[str, str]],
) -> str:
    """获取某组的评估产物文本。

    通用五维：拼接所有可用交付物（demand + 设定 + 大纲 + 正文摘要）。
    其他组：取对应 agent 的交付物。
    """
    if group_name == "general":
        # 通用五维基于全部交付物，但截断控制总长度
        parts: list[str] = []
        for agent in ("interview", "storybuilding", "detail-outline", "writing"):
            agent_text = eval_extractor.get_agent_delivery_text(deliveries, agent) if hasattr(eval_extractor, 'get_agent_delivery_text') else ""
            if not agent_text:
                # get_agent_delivery_text 需要 trace_id，这里直接从 deliveries 拼
                files = deliveries.get(agent, {})
                if files:
                    agent_text = "\n\n---\n\n".join(
        f"## 文件: {p}\n\n{c[:2000]}" for p, c in sorted(files.items())
                    )
            if agent_text:
                parts.append(f"### {agent} 交付物\n{agent_text[:3000]}")
        return "\n\n".join(parts)

    # 其他组：取对应 agent 交付物
    agent_map = {"storybuilding": "storybuilding", "outline": "detail-outline", "body": "writing"}
    agent = agent_map.get(group_name)
    if not agent:
        return ""
    files = deliveries.get(agent, {})
    if not files:
        return ""
    return "\n\n---\n\n".join(
        f"## 文件: {p}\n\n{c[:6000]}" for p, c in sorted(files.items())
    )


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


def _save_scores(trace_id: str, group_results: dict[str, dict[str, Any]]) -> None:
    """写 evaluation_scores 表（layer=组名，target=组名，metric=维度名）。"""
    now = datetime.now(UTC).isoformat()
    rows: list[tuple[Any, ...]] = []

    for group_name, result in group_results.items():
        if result.get("skipped"):
            continue
        evidence = result.get("evidence", "")
        for metric, score in result.get("scores", {}).items():
            rows.append((
                trace_id, group_name, group_name, metric,
                float(score) if isinstance(score, (int, float)) else 0.0,
                evidence, now,
            ))

    if rows:
        db.executemany(
            """INSERT INTO evaluation_scores
               (trace_id, layer, target, metric, score, evidence, scored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def _detect_badcase(group_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """badcase 判定：任一维度 < 3（五级制阈值）即 badcase。

    score=0（无法判断）不计入 badcase 判定（证据不足不等于质量差）。
    """
    threshold = rubric.BADCASE_THRESHOLD
    flagged: list[dict[str, Any]] = []

    for group_name, result in group_results.items():
        if result.get("skipped"):
            continue
        evidence = result.get("evidence", "")
        for metric, score in result.get("scores", {}).items():
            if not isinstance(score, (int, float)):
                continue
            if score == 0:
                continue  # 无法判断不计入
            if score < threshold:
                flagged.append({
                    "group": group_name,
                    "metric": metric,
                    "score": float(score),
                    "threshold": threshold,
                    "evidence": evidence,
                })

    return {
        "is_badcase": len(flagged) > 0,
        "flagged_dimensions": flagged,
    }


def evaluate_from_facts(facts: dict[str, Any], trace_id: str) -> dict[str, Any]:
    """从证据卷宗 facts 评估内容质量（阶段 C：不读工作区文件系统）。

    与 evaluate_trace 的区别：
      - 输入是卷宗 facts（B1 冻结的 deliveries），不再调 extract_deliveries 读文件
      - 不写 evaluation_runs / evaluation_scores 表（那些是旧 trace 维度表）
      - 返回结构相同（groups/badcase/calibration/rubric_version），供评估卷宗封存

    deliveries 结构转换：卷宗 facts.deliveries 是 {agent: {path: {content_frozen,...}}}，
    scoring 期望 {agent: {path: content_str}}，这里取 content_frozen 还原。
    """
    if not llm.judge_enabled():
        logger.warning("evaluate_from_facts 跳过：LLM 未配置")
        return {"skipped": True, "reason": "LLM 未配置"}

    # 从卷宗 facts 还原 scoring 期望的 deliveries 格式
    frozen = facts.get("deliveries") or {}
    deliveries: dict[str, dict[str, str]] = {}
    for agent, files in frozen.items():
        agent_files: dict[str, str] = {}
        for path, meta in files.items():
            content = meta.get("content_frozen") if isinstance(meta, dict) else meta
            if content:
                agent_files[path] = content
        if agent_files:
            deliveries[agent] = agent_files

    if not deliveries:
        return {"skipped": True, "reason": "卷宗无冻结交付物"}

    try:
        groups = rubric.applicable_groups(deliveries)
        group_results: dict[str, dict[str, Any]] = {}
        for group_name, dimensions in groups.items():
            group_results[group_name] = _evaluate_group(trace_id, group_name, dimensions, deliveries)

        badcase = _detect_badcase(group_results)
        return {
            "groups": group_results,
            "badcase": badcase,
            "calibration": rubric.CALIBRATION_STATUS,
            "rubric_version": rubric.RUBRIC_VERSION,
        }
    except Exception as exc:
        logger.exception("evaluate_from_facts 失败 trace=%s", trace_id)
        return {"error": str(exc)}


__all__ = ["evaluate_trace", "evaluate_from_facts"]
