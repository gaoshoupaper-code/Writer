"""反思自动归纳（数据闭环设计 D2/A8/D19）。

评估完成后，若 trace 为 badcase，从评估结果中归纳"失败模式"写入 reflection_library。
进化 Agent 启动时按问题分类查询注入上下文（Reflexion/ExpeL 式）。

归纳来源（两个维度）：
  1. scoring 的 badcase flagged_dimensions（低分维度：score < threshold）
     → category = metric（如"爽点密度"/"节奏"），pattern = "该维度得分 X 低于阈值 Y"
  2. eval_agent 的 findings（诊断问题，含 dimension/severity/finding/evidence）
     → category = dimension，pattern = finding 描述

调用方式：
  - scoring.evaluate_trace 完成后调 extract_from_eval(trace_id, result)
  - eval_agent 的 report 完成后调 extract_from_findings(trace_id, findings)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.reflection import repo

logger = logging.getLogger("evolution.reflection.extractor")


def extract_from_eval(trace_id: str, eval_result: dict[str, Any]) -> int:
    """从 scoring.evaluate_trace 的 badcase 结果归纳反思。

    Returns:
        新增/合并的反思条数。
    """
    badcase = eval_result.get("badcase", {})
    if not badcase.get("is_badcase"):
        return 0

    flagged = badcase.get("flagged_dimensions", [])
    if not flagged:
        return 0

    count = 0
    for flag in flagged:
        category = flag.get("metric") or flag.get("target") or "unknown"
        score = flag.get("score", 0)
        threshold = flag.get("threshold", 0)
        layer = flag.get("layer", "")
        target = flag.get("target", "")
        evidence = flag.get("evidence", "")[:200]

        pattern = (
            f"[{layer}/{target}] {category} 得分 {score:.2f} 低于阈值 {threshold:.2f}"
        )
        symptom = f"评估分数 {score:.2f}（阈值 {threshold:.2f}）"
        suggestion = evidence or "需针对该维度改进"

        repo.merge_reflection(
            category=category,
            pattern=pattern,
            symptom=symptom,
            suggestion=suggestion,
            source_trace_id=trace_id,
        )
        count += 1

    logger.info("从 trace %s 的 badcase 归纳 %d 条反思", trace_id, count)
    return count


def extract_from_findings(trace_id: str, findings: list[dict[str, Any]]) -> int:
    """从 eval_agent 的 findings（诊断问题）归纳反思。

    findings 结构：[{dimension, severity, evidence_type, finding, evidence}]
    只归纳 severity=high/medium 的（低优先级不值得反思）。
    """
    if not findings:
        return 0

    count = 0
    for f in findings:
        if not isinstance(f, dict):
            continue
        severity = f.get("severity", "")
        if severity not in ("high", "medium"):
            continue

        category = f.get("dimension") or "unknown"
        finding_text = f.get("finding", "")
        if not finding_text:
            continue
        evidence = f.get("evidence", "")[:200]

        pattern = finding_text[:300]
        symptom = f"[{severity}] {f.get('evidence_type', '')}"
        suggestion = evidence or "参见评估报告详情"

        repo.merge_reflection(
            category=category,
            pattern=pattern,
            symptom=symptom,
            suggestion=suggestion,
            source_trace_id=trace_id,
        )
        count += 1

    if count:
        logger.info("从 trace %s 的 findings 归纳 %d 条反思", trace_id, count)
    return count


def extract_after_scoring(trace_id: str) -> int:
    """在 scoring 完成后调用：从 evaluation_sessions + badcase + memory_quality 提取反思。

    集成入口：scoring.evaluate_trace 的末尾或 eval_agent 完成后调此函数。
    拉取该 trace 的 evaluation_sessions.findings_json + scores + memory_quality，统一归纳。
    """
    import app.core.db as db

    # 查 evaluation_sessions
    session = db.query_one(
        "SELECT * FROM evaluation_sessions WHERE trace_id=? AND status='done' "
        "ORDER BY updated_at DESC LIMIT 1",
        (trace_id,),
    )

    total = 0

    if session:
        # 1. findings（诊断问题）
        findings_raw = session.get("findings_json")
        if findings_raw:
            try:
                findings = json.loads(findings_raw) if isinstance(findings_raw, str) else findings_raw
                total += extract_from_findings(trace_id, findings)
            except (json.JSONDecodeError, TypeError):
                pass

        # 2. badcase（从 evaluation_scores 反查低分维度）
        scores = db.query_all(
            "SELECT layer, target, metric, score FROM evaluation_scores WHERE trace_id=?",
            (trace_id,),
        )
        if scores:
            from app.eval_agent.rubrics import xianxia as rubric
            flagged = []
            content_thresh = rubric.CONTENT_BADCASE_THRESHOLD
            sub_thresh = rubric.SUBAGENT_BADCASE_THRESHOLD
            for s in scores:
                thresh = content_thresh if s["layer"] == "content" else sub_thresh
                if s["score"] < thresh:
                    flagged.append({
                        "layer": s["layer"], "target": s["target"],
                        "metric": s["metric"], "score": s["score"], "threshold": thresh,
                    })
            if flagged:
                total += extract_from_eval(trace_id, {"badcase": {"is_badcase": True, "flagged_dimensions": flagged}})

    # 3. 记忆质量失败模式（P4：从 trace run_meta 事件读 memory_quality）
    total += extract_from_memory_quality(trace_id)

    return total


# ── 记忆质量失败模式归纳（P4 进化闭环）────────────────────────────


def extract_from_memory_quality(trace_id: str) -> int:
    """从 trace 的 run_meta 事件读 memory_quality，归纳记忆系统失败模式。

    memory_recall middleware 每次检索后写一条 run_meta 事件（含 memory_quality dict）。
    本函数扫描这些事件，归纳失败模式写入 reflection_library。

    失败模式类别（设计方案 §7.3 扩展 2）：
      - recall_miss：召回未命中关键设定（证据包为空/节点极少）
      - retrieval_fail：检索异常（retrieval_ok=False）

    Returns:
        新增/合并的反思条数。
    """
    import app.core.db as db

    # 查 trace 的 run_meta 事件（memory_quality 埋点）
    rows = db.query_all(
        "SELECT payload_json FROM event_payloads "
        "WHERE trace_id=? AND type='run_meta' ORDER BY sequence",
        (trace_id,),
    )
    if not rows:
        return 0

    count = 0
    for row in rows:
        try:
            payload = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]
        except (json.JSONDecodeError, TypeError):
            continue

        input_data = payload.get("input") or {}
        mq = input_data.get("memory_quality")
        if not mq or not isinstance(mq, dict):
            continue

        ok = mq.get("retrieval_ok", True)
        nodes_count = mq.get("evidence_nodes_count", 0)
        edges_count = mq.get("evidence_edges_count", 0)
        chapter = mq.get("chapter_num", "?")
        error = mq.get("error")

        if not ok:
            # 检索异常
            repo.merge_reflection(
                category="retrieval_fail",
                pattern=f"第{chapter}章记忆检索异常：{error or '未知错误'}",
                symptom=f"retrieval_ok=False, chapter={chapter}",
                suggestion="检查 FalkorDB 连通性 + memory_recall_middleware 健康检查逻辑",
                source_trace_id=trace_id,
            )
            count += 1
        elif nodes_count == 0 and edges_count == 0:
            # 召回为空（图谱可能没入图，或查询条件不匹配）
            repo.merge_reflection(
                category="recall_miss",
                pattern=f"第{chapter}章记忆召回为空（0 节点 0 边）",
                symptom=f"evidence_nodes=0, evidence_edges=0, chapter={chapter}",
                suggestion="检查 storybuilding 是否入图 + 查询条件构造是否匹配图谱内容",
                source_trace_id=trace_id,
            )
            count += 1

    if count:
        logger.info("从 trace %s 的 memory_quality 归纳 %d 条记忆失败模式反思", trace_id, count)
    return count


__all__ = [
    "extract_from_eval",
    "extract_from_findings",
    "extract_after_scoring",
    "extract_from_memory_quality",
]
