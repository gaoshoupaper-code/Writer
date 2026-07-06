"""promote_tasks 表的数据访问层 + 状态机（数据闭环设计 B1）。

状态流转：
  pending       — 新建（judge_scheduler 扫到未 judge 的生产 trace 时创建）
  judging       — judge 调用中
  needs_confirm — judge 完成，verdict=auto_promote 或 needs_human（待人工确认）
  rejected      — 人工拒绝 / judge verdict=auto_reject
  promoted      — 人工 accept，已入 growing

promote_tasks 与 evaluation_runs 的区别：
  evaluation_runs 是 eval_agent 的幂等标记（同 trace 不重评），面向"评估诊断"。
  promote_tasks 是 promote 闸门的任务记录，面向"是否入数据集"，含人工决策。
  两者通过 trace_id 关联，judge 复用 eval_agent/scoring 的打分结果。
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import app.core.db as db

# 状态常量
STATUS_PENDING = "pending"
STATUS_JUDGING = "judging"
STATUS_NEEDS_CONFIRM = "needs_confirm"
STATUS_REJECTED = "rejected"
STATUS_PROMOTED = "promoted"

# judge 裁决（自动判定）
VERDICT_AUTO_PROMOTE = "auto_promote"    # 高分，但仍需人工确认（A10）
VERDICT_NEEDS_HUMAN = "needs_human"      # 边界，进人工队列
VERDICT_AUTO_REJECT = "auto_reject"      # 低分，自动丢

# 人工决策
DECISION_ACCEPT = "accept"
DECISION_REJECT = "reject"

# judge 阈值（内容层 overall 分数，0~1）
# ≥ AUTO_PROMOTE_THRESHOLD → auto_promote（仍需确认，A10）
# < AUTO_REJECT_THRESHOLD  → auto_reject
# 中间 → needs_human
AUTO_PROMOTE_THRESHOLD = 0.8
AUTO_REJECT_THRESHOLD = 0.4


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


# ── 创建 ────────────────────────────────────────────────────


def create_task(*, trace_id: str, owner_user_id: str | None = None) -> str:
    """为一条生产 trace 创建 promote 任务（pending 状态）。

    幂等：同 trace_id 已有任务则不重复创建，返回已有 task_id。
    """
    existing = db.query_one(
        "SELECT task_id FROM promote_tasks WHERE trace_id=?",
        (trace_id,),
    )
    if existing:
        return existing["task_id"]

    task_id = _new_id()
    db.execute(
        """INSERT INTO promote_tasks
           (task_id, trace_id, owner_user_id, status, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (task_id, trace_id, owner_user_id, STATUS_PENDING, _now()),
    )
    return task_id


# ── 状态流转 ────────────────────────────────────────────────


def mark_judging(task_id: str) -> None:
    """标记为 judging（judge 调用前）。"""
    db.execute(
        "UPDATE promote_tasks SET status=? WHERE task_id=?",
        (STATUS_JUDGING, task_id),
    )


def set_judge_result(
    task_id: str,
    *,
    scores: dict[str, Any],
    verdict: str,
) -> None:
    """写入 judge 结果，根据 verdict 推进状态。

    - auto_promote / needs_human → needs_confirm（待人工确认，A10）
    - auto_reject → rejected
    """
    if verdict == VERDICT_AUTO_REJECT:
        new_status = STATUS_REJECTED
    else:
        new_status = STATUS_NEEDS_CONFIRM

    db.execute(
        """UPDATE promote_tasks
           SET status=?, judge_scores=?, judge_verdict=?
           WHERE task_id=?""",
        (new_status, json.dumps(scores, ensure_ascii=False), verdict, task_id),
    )


def submit_decision(
    task_id: str,
    *,
    decision: str,
    annotator: str,
    target_case_id: str | None = None,
    new_case_title: str | None = None,
) -> None:
    """人工提交决策（accept / reject）。

    accept 时调用方需额外传入 target_case_id（归入已有）或 new_case_title（新建）。
    实际入库 growing 由 promote.py 执行，本函数只记录决策。
    """
    if decision == DECISION_ACCEPT:
        new_status = STATUS_PROMOTED
    else:
        new_status = STATUS_REJECTED

    db.execute(
        """UPDATE promote_tasks
           SET status=?, annotator=?, decision=?, target_case_id=?, new_case_title=?, decided_at=?
           WHERE task_id=?""",
        (new_status, annotator, decision, target_case_id, new_case_title, _now(), task_id),
    )


# ── 查询 ────────────────────────────────────────────────────


def get(task_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM promote_tasks WHERE task_id=?", (task_id,))


def get_by_trace(trace_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM promote_tasks WHERE trace_id=?", (trace_id,))


def list_tasks(
    *,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """列任务（created_at 倒序，分页）。status=None 表示全部非终态。"""
    where = ""
    params: list[Any] = []
    if status:
        where = "WHERE status=?"
        params.append(status)
    else:
        # 默认只看活跃态（pending/judging/needs_confirm）
        where = "WHERE status IN ('pending','judging','needs_confirm')"

    total_row = db.query_one(f"SELECT COUNT(*) AS n FROM promote_tasks {where}", tuple(params))
    total = total_row["n"] if total_row else 0

    offset = (page - 1) * page_size
    params.extend([page_size, offset])
    rows = db.query_all(
        f"""SELECT * FROM promote_tasks {where}
            ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        tuple(params),
    )
    return rows, total


def list_pending_judge(limit: int = 20) -> list[dict[str, Any]]:
    """列待 judge 的任务（judge_scheduler 用）。

    待 judge = promote_tasks pending 且 trace 在 runs 表里 status=completed
    （跑完才有产出可 judge）。
    """
    return db.query_all(
        """SELECT pt.* FROM promote_tasks pt
           JOIN runs r ON pt.trace_id = r.trace_id
           WHERE pt.status = ? AND r.status = 'completed'
           ORDER BY pt.created_at ASC
           LIMIT ?""",
        (STATUS_PENDING, limit),
    )


__all__ = [
    # 状态常量
    "STATUS_PENDING", "STATUS_JUDGING", "STATUS_NEEDS_CONFIRM",
    "STATUS_REJECTED", "STATUS_PROMOTED",
    # verdict 常量
    "VERDICT_AUTO_PROMOTE", "VERDICT_NEEDS_HUMAN", "VERDICT_AUTO_REJECT",
    # 决策常量
    "DECISION_ACCEPT", "DECISION_REJECT",
    # 阈值
    "AUTO_PROMOTE_THRESHOLD", "AUTO_REJECT_THRESHOLD",
    # CRUD
    "create_task", "mark_judging", "set_judge_result", "submit_decision",
    "get", "get_by_trace", "list_tasks", "list_pending_judge",
]
