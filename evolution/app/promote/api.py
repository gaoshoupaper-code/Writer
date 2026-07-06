"""Promote 标注 API（数据闭环设计 B6）。

端点：
  GET  /api/promote/tasks              标注队列列表（按 status 过滤，分页）
  GET  /api/promote/tasks/{task_id}    标注详情（trace 摘要 + judge 分数）
  POST /api/promote/tasks/{task_id}/decide  提交标注决策（accept/reject + 归类）

标注者只做两件事（D6）：收/不收 + 归类（入哪个 case / 新建 case）。
accept 时：
  - target_case_id 指定 → 归入已有 growing case
  - new_case_title + demand_md → 新建 growing case
  - reference_output 可选 → 存编辑终稿
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.common import evalset
from app.promote import repo, promote

logger = logging.getLogger("evolution.promote.api")

router = APIRouter(prefix="/promote", tags=["promote"])


# ── 列表 ────────────────────────────────────────────────────


@router.get("/tasks")
def list_tasks(
    status: str | None = Query(None, description="pending/judging/needs_confirm/rejected/promoted/空=活跃态"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """标注队列列表（created_at 倒序，分页）。"""
    rows, total = repo.list_tasks(status=status, page=page, page_size=page_size)
    tasks = [_row_to_summary(r) for r in rows]
    return {"tasks": tasks, "total": total, "page": page, "page_size": page_size}


@router.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    """标注详情：trace 摘要 + judge 分数 + 交付物概要。"""
    task = repo.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    trace_id = task["trace_id"]
    detail = _row_to_detail(task)

    # 补充 trace 摘要（runs 表）
    import app.core.db as db
    run = db.query_one("SELECT * FROM runs WHERE trace_id=?", (trace_id,))
    if run:
        detail["trace"] = {
            "trace_id": trace_id,
            "status": run["status"],
            "owner_user_id": run["owner_user_id"],
            "started_at": run["started_at"],
            "ended_at": run["ended_at"],
            "duration_ms": run["duration_ms"],
            "session_name": run["session_name"],
        }

    # 交付物概要（让标注者知道写了什么）
    try:
        from app.eval_agent import eval_extractor
        detail["deliveries"] = eval_extractor.summarize_deliveries(trace_id)
    except Exception:
        detail["deliveries"] = {}

    return detail


# ── 决策 ────────────────────────────────────────────────────


class DecideRequest(BaseModel):
    """标注决策请求（D6：收/不收 + 归类）。"""
    decision: str                        # accept | reject
    annotator: str = "anonymous"
    # accept 归入已有 case
    target_case_id: str | None = None
    # accept 新建 case
    new_case_title: str | None = None
    demand_md: str | None = None
    # 编辑终稿（可选，存为 reference.md）
    reference_output: str | None = None


@router.post("/tasks/{task_id}/decide")
def decide(task_id: str, req: DecideRequest) -> dict[str, Any]:
    """提交标注决策。

    accept：
      - target_case_id → 归入已有 growing case（补 reference.md）
      - new_case_title + demand_md → 新建 growing case
      - reference_output 可选 → 编辑终稿
    reject：
      - 标记 rejected，不入数据集
    """
    task = repo.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    if task["status"] not in (repo.STATUS_NEEDS_CONFIRM, repo.STATUS_PENDING):
        raise HTTPException(
            status_code=409,
            detail=f"task 状态 {task['status']} 不可决策（需 needs_confirm）",
        )

    if req.decision == "reject":
        repo.submit_decision(
            task_id, decision=repo.DECISION_REJECT, annotator=req.annotator,
        )
        return {"task_id": task_id, "status": "rejected"}

    if req.decision != "accept":
        raise HTTPException(status_code=400, detail="decision must be accept|reject")

    # accept：校验归类参数
    if not req.target_case_id and not req.new_case_title:
        raise HTTPException(
            status_code=400,
            detail="accept 需指定 target_case_id（归入）或 new_case_title（新建）",
        )

    # 归入已有 case：校验存在
    if req.target_case_id and not evalset.case_exists(req.target_case_id, layer="growing"):
        raise HTTPException(
            status_code=400,
            detail=f"target case {req.target_case_id} 不在 growing 层",
        )

    # 尝试从 trace 提取编辑终稿（如果标注者没手动提供）
    reference = req.reference_output
    if reference is None:
        reference = promote.extract_reference_from_trace(task["trace_id"])

    # 执行入库 growing
    case_id = promote.promote_to_growing(
        trace_id=task["trace_id"],
        target_case_id=req.target_case_id,
        new_case_title=req.new_case_title,
        demand_md=req.demand_md,
        reference_output=reference,
    )

    # 记录决策
    repo.submit_decision(
        task_id,
        decision=repo.DECISION_ACCEPT,
        annotator=req.annotator,
        target_case_id=req.target_case_id,
        new_case_title=req.new_case_title,
    )

    return {
        "task_id": task_id,
        "status": "promoted",
        "case_id": case_id,
        "has_reference": reference is not None,
    }


# ── 辅助：行 → 摘要/详情 ───────────────────────────────────


def _row_to_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "trace_id": row["trace_id"],
        "owner_user_id": row["owner_user_id"],
        "status": row["status"],
        "judge_verdict": row["judge_verdict"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


def _row_to_detail(row: dict[str, Any]) -> dict[str, Any]:
    detail = _row_to_summary(row)
    # judge 分数 JSON 解析
    scores_raw = row.get("judge_scores")
    if scores_raw:
        try:
            detail["judge_scores"] = json.loads(scores_raw)
        except (json.JSONDecodeError, TypeError):
            detail["judge_scores"] = {"raw": scores_raw}
    else:
        detail["judge_scores"] = None
    detail["annotator"] = row.get("annotator")
    detail["decision"] = row.get("decision")
    detail["target_case_id"] = row.get("target_case_id")
    return detail


__all__ = ["router"]
