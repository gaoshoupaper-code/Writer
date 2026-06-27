"""双层评估 API 路由（Phase 1 T1.7）。

端点：
  - GET  /evaluation/{trace_id}           查某 trace 的双层评估分数
  - POST /evaluation/{trace_id}           手动触发/重跑某 trace 的双层评估
  - GET  /evaluation/{trace_id}/badcase   查 badcase 标记
  - GET  /evaluation/{trace_id}/deliveries 查交付物概要（诊断用）

设计依据：设计文档 Phase 1 T1.7。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

import app.core.db as db

router = APIRouter(tags=["evaluation"])


# ── 字面路径必须先于 /evaluation/{trace_id} 注册，否则会被 {trace_id} 吞掉 ──

@router.get("/evaluation/stats/overview")
def evaluation_overview() -> dict[str, Any]:
    """评估大盘：已评估 trace 数 / badcase 数 / 各维度均分。

    含双层均分聚合（content_avg / subagent_avg）+ 阈值，供前端大盘卡片直接展示。
    """
    from app.diagnosis.rubrics import xianxia as rubric

    total = db.query_one("SELECT count(*) AS c FROM evaluation_runs WHERE status='done'")
    badcase_traces = db.query_all(
        """SELECT trace_id, count(*) AS flagged FROM evaluation_scores
           WHERE (layer='content' AND score < ?) OR (layer='subagent' AND score < ?)
           GROUP BY trace_id""",
        (rubric.CONTENT_BADCASE_THRESHOLD, rubric.SUBAGENT_BADCASE_THRESHOLD),
    )
    # 各维度均分
    avg = db.query_all(
        """SELECT layer, target, metric, AVG(score) AS avg_score
           FROM evaluation_scores GROUP BY layer, target, metric
           ORDER BY layer, target"""
    )
    content_avg_rows = [d for d in avg if d["layer"] == "content"]
    subagent_avg_rows = [d for d in avg if d["layer"] == "subagent"]
    content_avg = (sum(d["avg_score"] for d in content_avg_rows) / len(content_avg_rows)) if content_avg_rows else 0
    subagent_avg = (sum(d["avg_score"] for d in subagent_avg_rows) / len(subagent_avg_rows)) if subagent_avg_rows else 0
    return {
        "evaluated_count": total["c"] if total else 0,
        "badcase_count": len(badcase_traces),
        "dimension_averages": avg,
        "content_avg": content_avg,
        "subagent_avg": subagent_avg,
        "content_threshold": rubric.CONTENT_BADCASE_THRESHOLD,
        "subagent_threshold": rubric.SUBAGENT_BADCASE_THRESHOLD,
    }


@router.get("/evaluation/list")
def evaluation_list(limit: int = 100) -> list[dict[str, Any]]:
    """已评估 trace 列表：每条带双层均分 + badcase 维度数 + 评估时间。

    供前端评估列表页渲染（对应旧 Jinja2 /evaluation 的 evaluated_traces 查询）。
    """
    from app.diagnosis.rubrics import xianxia as rubric

    rows = db.query_all(
        f"""SELECT er.trace_id, er.status AS eval_status, er.finished_at AS evaluated_at,
            (SELECT AVG(score) FROM evaluation_scores WHERE trace_id=er.trace_id AND layer='content') AS content_avg,
            (SELECT AVG(score) FROM evaluation_scores WHERE trace_id=er.trace_id AND layer='subagent') AS subagent_avg,
            (SELECT count(*) FROM evaluation_scores WHERE trace_id=er.trace_id
             AND ((layer='content' AND score < {rubric.CONTENT_BADCASE_THRESHOLD})
               OR (layer='subagent' AND score < {rubric.SUBAGENT_BADCASE_THRESHOLD}))) AS badcase_count
            FROM evaluation_runs er WHERE er.status='done'
            ORDER BY er.finished_at DESC LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in rows]


@router.get("/evaluation/{trace_id}")
def get_evaluation(trace_id: str) -> dict[str, Any]:
    """查某 trace 的双层评估分数。

    返回 {content: [...], subagent: [...], run_status}。
    无评估记录返回 404。
    """
    run_status = db.query_one(
        "SELECT status, error FROM evaluation_runs WHERE trace_id = ?", (trace_id,)
    )
    if run_status is None:
        raise HTTPException(status_code=404, detail="无评估记录")

    scores = db.query_all(
        "SELECT layer, target, metric, score, evidence, scored_at "
        "FROM evaluation_scores WHERE trace_id = ? ORDER BY layer, target, metric",
        (trace_id,),
    )
    content = [s for s in scores if s["layer"] == "content"]
    subagent = [s for s in scores if s["layer"] == "subagent"]
    return {
        "trace_id": trace_id,
        "run_status": run_status["status"],
        "run_error": run_status["error"],
        "content": content,
        "subagent": subagent,
    }


@router.post("/evaluation/{trace_id}")
def trigger_evaluation(trace_id: str) -> dict[str, Any]:
    """手动触发/重跑某 trace 的双层评估（忽略幂等会重评）。

    先删该 trace 的 evaluation_runs + evaluation_scores 记录强制重评。
    """
    # 确认 trace 存在
    run = db.query_one("SELECT trace_id FROM runs WHERE trace_id = ?", (trace_id,))
    if run is None:
        raise HTTPException(status_code=404, detail="trace 不存在")

    from app.diagnosis.evaluation import evaluate_trace
    from app.core.llm import judge_enabled
    if not judge_enabled():
        raise HTTPException(status_code=400, detail="LLM 未配置（JUDGE_MODEL/JUDGE_API_KEY）")

    # 强制重评：删旧记录
    db.execute("DELETE FROM evaluation_runs WHERE trace_id = ?", (trace_id,))
    db.execute("DELETE FROM evaluation_scores WHERE trace_id = ?", (trace_id,))

    result = evaluate_trace(trace_id)
    if result is None:
        raise HTTPException(status_code=500, detail="评估失败，见日志")
    return {"trace_id": trace_id, "result": result}


@router.get("/evaluation/{trace_id}/badcase")
def get_badcase(trace_id: str) -> dict[str, Any]:
    """查某 trace 的 badcase 标记（哪些维度低于阈值）。

    实时从 evaluation_scores 计算（不单独存 badcase 表，保持单一数据源）。
    """
    from app.diagnosis.rubrics import xianxia as rubric

    scores = db.query_all(
        "SELECT layer, target, metric, score FROM evaluation_scores WHERE trace_id = ?",
        (trace_id,),
    )
    if not scores:
        raise HTTPException(status_code=404, detail="无评估记录")

    flagged: list[dict[str, Any]] = []
    for s in scores:
        threshold = (
            rubric.CONTENT_BADCASE_THRESHOLD
            if s["layer"] == "content"
            else rubric.SUBAGENT_BADCASE_THRESHOLD
        )
        if s["score"] < threshold:
            flagged.append({
                "layer": s["layer"], "target": s["target"],
                "metric": s["metric"], "score": s["score"], "threshold": threshold,
            })
    return {"trace_id": trace_id, "is_badcase": len(flagged) > 0, "flagged": flagged}


@router.get("/evaluation/{trace_id}/deliveries")
def get_deliveries(trace_id: str) -> dict[str, Any]:
    """查某 trace 各 subagent 的交付物概要（诊断用，不含正文）。"""
    from app.diagnosis.eval_extractor import summarize_deliveries
    summary = summarize_deliveries(trace_id)
    return {"trace_id": trace_id, "deliveries": summary}
