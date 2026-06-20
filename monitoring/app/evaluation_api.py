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

import app.db as db

router = APIRouter(tags=["evaluation"])


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

    from app.evaluation import evaluate_trace
    from app.llm import judge_enabled
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
    from app.rubrics import xianxia as rubric

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
    from app.eval_extractor import summarize_deliveries
    summary = summarize_deliveries(trace_id)
    return {"trace_id": trace_id, "deliveries": summary}


@router.get("/evaluation/stats/overview")
def evaluation_overview() -> dict[str, Any]:
    """评估大盘：已评估 trace 数 / badcase 数 / 各维度均分。"""
    from app.rubrics import xianxia as rubric

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
    return {
        "evaluated_count": total["c"] if total else 0,
        "badcase_count": len(badcase_traces),
        "dimension_averages": avg,
    }


# ── Phase 2：诊断与候选 ──


@router.get("/candidates")
def list_candidates(status: str | None = None) -> list[dict[str, Any]]:
    """列改进候选（improvement_candidates）。

    query param status 可选过滤（pending/optimized/ab_testing/approved/rejected）。
    """
    from app.diagnosis import list_candidates as _list
    return _list(status)


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: int) -> dict[str, Any]:
    """查单个候选详情（含诊断结论 + 候选 prompt 版本内容）。"""
    cand = db.query_one("SELECT * FROM improvement_candidates WHERE id=?", (candidate_id,))
    if cand is None:
        raise HTTPException(status_code=404, detail="候选不存在")
    result = dict(cand)
    # 附带候选版本内容（若有）
    if cand["candidate_version_id"]:
        import app.prompts_repo as repo
        ver = db.query_one(
            "SELECT * FROM prompt_versions WHERE id=?", (cand["candidate_version_id"],)
        )
        if ver:
            result["candidate_version"] = {
                "version": ver["version"], "content": ver["content"],
                "commit_message": ver["commit_message"],
            }
    return result


@router.post("/candidates/{candidate_id}/optimize")
def trigger_optimize(candidate_id: int) -> dict[str, Any]:
    """手动（重新）生成某候选的改进版 prompt（忽略幂等会重生成）。

    自动连锁已在评估时生成过；此端点用于重试/手动触发。
    """
    from app.llm import judge_enabled
    if not judge_enabled():
        raise HTTPException(status_code=400, detail="LLM 未配置")
    cand = db.query_one("SELECT * FROM improvement_candidates WHERE id=?", (candidate_id,))
    if cand is None:
        raise HTTPException(status_code=404, detail="候选不存在")
    # 重置已生成的版本，强制重新优化
    db.execute(
        "UPDATE improvement_candidates SET candidate_version_id=NULL, status='pending' WHERE id=?",
        (candidate_id,),
    )
    from app.optimizer import optimize_candidate
    result = optimize_candidate(candidate_id)
    if result is None:
        raise HTTPException(status_code=500, detail="优化失败，见日志")
    return {"candidate_id": candidate_id, "result": result}
