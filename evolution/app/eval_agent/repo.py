"""evaluation_sessions 表的 CRUD（决策 S4/T6）。

评估 Agent 的 session 记录。评估报告（scores/findings/report）直接落库，
作为进化 Agent 的 DB 交接通道（S2）——进化启动时按 trace_id 查 done 评估。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import app.core.db as db


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_session(
    eval_id: str,
    trace_id: str,
    *,
    agent_version_type: str | None = None,
    agent_version_id: int | None = None,
) -> None:
    """创建一个评估 session（启动时调用）。"""
    db.execute(
        """INSERT INTO evaluation_sessions
           (eval_id, trace_id, agent_version_type, agent_version_id,
            status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'running', ?, ?)""",
        (eval_id, trace_id, agent_version_type, agent_version_id, _now(), _now()),
    )


def update_session(
    eval_id: str,
    *,
    status: str | None = None,
    scores: dict[str, Any] | None = None,
    findings: list[dict[str, Any]] | None = None,
    report_md: str | None = None,
) -> None:
    """更新评估 session 字段（只更新非 None 的字段）。

    scores/findings 序列化为 JSON 存库；report_md 内联全文。
    """
    sets: list[str] = []
    params: list[Any] = []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if scores is not None:
        sets.append("scores_json = ?")
        params.append(json.dumps(scores, ensure_ascii=False))
    if findings is not None:
        sets.append("findings_json = ?")
        params.append(json.dumps(findings, ensure_ascii=False))
    if report_md is not None:
        sets.append("report_md = ?")
        params.append(report_md)

    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(eval_id)

    db.execute(
        f"UPDATE evaluation_sessions SET {', '.join(sets)} WHERE eval_id = ?",
        tuple(params),
    )


def _deserialize(row: dict[str, Any]) -> dict[str, Any]:
    """行反序列化：scores_json/findings_json 解析为 dict/list。"""
    if not row:
        return row  # type: ignore[return-value]
    if row.get("scores_json"):
        try:
            row["scores"] = json.loads(row["scores_json"])
        except (json.JSONDecodeError, TypeError):
            row["scores"] = None
    else:
        row["scores"] = None
    if row.get("findings_json"):
        try:
            row["findings"] = json.loads(row["findings_json"])
        except (json.JSONDecodeError, TypeError):
            row["findings"] = None
    else:
        row["findings"] = None
    return row


def get_session(eval_id: str) -> dict[str, Any] | None:
    """查单个评估 session（含 scores/findings/report_md）。"""
    row = db.query_one(
        "SELECT * FROM evaluation_sessions WHERE eval_id = ?",
        (eval_id,),
    )
    return _deserialize(row) if row else None


def list_sessions(
    *,
    trace_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列评估 session（最新在前）。可按 trace_id 过滤。"""
    if trace_id:
        rows = db.query_all(
            """SELECT * FROM evaluation_sessions
               WHERE trace_id = ? ORDER BY created_at DESC LIMIT ?""",
            (trace_id, limit),
        )
    else:
        rows = db.query_all(
            "SELECT * FROM evaluation_sessions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return [_deserialize(dict(r)) for r in rows]


def get_done_by_trace(trace_id: str) -> dict[str, Any] | None:
    """查某 trace 最近一条 done 评估（进化强前置校验用，T2/S8）。

    一条 trace 可能被多次评估（不同时间），取最新的 done 记录。
    """
    row = db.query_one(
        """SELECT * FROM evaluation_sessions
           WHERE trace_id = ? AND status = 'done'
           ORDER BY updated_at DESC LIMIT 1""",
        (trace_id,),
    )
    return _deserialize(row) if row else None


def list_evaluated_traces(limit: int = 100) -> list[dict[str, Any]]:
    """列已评估（有 done 记录）的 trace（进化入口「选已评估 trace」用）。

    返回每个 trace 最新 done 评估的摘要（eval_id/trace_id/scores 摘要/created_at）。
    """
    rows = db.query_all(
        """SELECT * FROM evaluation_sessions
           WHERE status = 'done'
           ORDER BY updated_at DESC LIMIT ?""",
        (limit,),
    )
    return [_deserialize(dict(r)) for r in rows]


__all__ = [
    "create_session",
    "update_session",
    "get_session",
    "list_sessions",
    "get_done_by_trace",
    "list_evaluated_traces",
]
