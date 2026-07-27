"""evaluation_sessions 表的 CRUD（决策 S4/T6）。

评估 Agent 的尝试记录。语义随「证据分级可见性重构」（2026-07-27）演化：
- 旧：评估结果行（scores/findings/report 内联，靠 trace_id 关联）。
- 新：评估尝试（承载任务生命周期 + 资源消耗）。评估产物（结论 + 引用证据）
  拆到独立的不可变 evaluation_dossiers 表，通过 sealed_dossier_id 关联。

本 repo 同时保留向后兼容：done 评估仍可直接查 scores/findings（旧链路只读保留）。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import app.core.db as db

# 评估尝试的活动状态（占用单任务锁，需求 §40：同卷宗最多一个活动任务）。
# 终态：completed / failed / stopped / interrupted（completed 才有 sealed_dossier_id）。
ACTIVE_STATUSES = ("running",)

# 评估尝试六态（需求 §48）。旧值域 running/done/failed/cancelled 沿用同一列：
# running→running / done→completed / failed→failed / cancelled→stopped。
# 六态完全切换在阶段 C 落地；当前 create/update 仍兼容旧值。
ATTEMPT_STATUSES = ("queued", "running", "completed", "failed", "stopped", "interrupted")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_session(
    eval_id: str,
    trace_id: str,
    *,
    agent_version_type: str | None = None,
    agent_version_id: int | None = None,
    bound_dossier_id: str | None = None,
) -> None:
    """创建一个评估尝试（启动时调用）。

    bound_dossier_id：阶段 C 评估按证据卷宗启动后写入，启动时锁定、不可变。
    """
    db.execute(
        """INSERT INTO evaluation_sessions
           (eval_id, trace_id, agent_version_type, agent_version_id,
            status, bound_dossier_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'running', ?, ?, ?)""",
        (eval_id, trace_id, agent_version_type, agent_version_id,
         bound_dossier_id, _now(), _now()),
    )


def update_session(
    eval_id: str,
    *,
    status: str | None = None,
    scores: dict[str, Any] | None = None,
    findings: list[dict[str, Any]] | None = None,
    report_md: str | None = None,
    sealed_dossier_id: str | None = None,
    model_calls_used: int | None = None,
    tokens_used: int | None = None,
    runtime_ms: int | None = None,
    failure_reason: str | None = None,
    stop_reason: str | None = None,
) -> None:
    """更新评估尝试字段（只更新非 None 的字段）。

    scores/findings 序列化为 JSON 存库；report_md 内联全文。
    资源消耗字段（model_calls_used/tokens_used/runtime_ms）用于阶段 F 资源上限展示。
    sealed_dossier_id：评估卷宗封存成功后回填（阶段 C）。
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
    if sealed_dossier_id is not None:
        sets.append("sealed_dossier_id = ?")
        params.append(sealed_dossier_id)
    if model_calls_used is not None:
        sets.append("model_calls_used = ?")
        params.append(model_calls_used)
    if tokens_used is not None:
        sets.append("tokens_used = ?")
        params.append(tokens_used)
    if runtime_ms is not None:
        sets.append("runtime_ms = ?")
        params.append(runtime_ms)
    if failure_reason is not None:
        sets.append("failure_reason = ?")
        params.append(failure_reason)
    if stop_reason is not None:
        sets.append("stop_reason = ?")
        params.append(stop_reason)

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
    """查某 trace 最近一条成功评估（进化强前置校验用，T2/S8）。

    阶段 C：兼容 done（旧链路）和 completed（新链路卷宗封存）。
    一条 trace 可能被多次评估（不同时间），取最新成功记录。
    """
    row = db.query_one(
        """SELECT * FROM evaluation_sessions
           WHERE trace_id = ? AND status IN ('done', 'completed')
           ORDER BY updated_at DESC LIMIT 1""",
        (trace_id,),
    )
    return _deserialize(row) if row else None


def list_evaluated_traces(limit: int = 100) -> list[dict[str, Any]]:
    """列已评估的 trace（进化入口「选已评估 trace」用）。

    阶段 C：评估成功状态从 done 变为 completed（评估卷宗封存）。兼容两者：
    done = 旧链路（直读 trace 评估）；completed = 新链路（卷宗评估 + 封存）。
    阶段 D 进化入口将改为按评估卷宗启动，此函数届时重写。
    """
    rows = db.query_all(
        """SELECT * FROM evaluation_sessions
           WHERE status IN ('done', 'completed')
           ORDER BY updated_at DESC LIMIT ?""",
        (limit,),
    )
    return [_deserialize(dict(r)) for r in rows]


# ── 评估尝试查询（2026-07-27 证据分级可见性重构）──────────────────────


def get_active_attempt_by_dossier(dossier_id: str) -> dict[str, Any] | None:
    """查某证据卷宗当前活动的评估尝试（需求 §40：单卷宗单活动任务）。

    用于启动评估时复用现有活动任务（不创建并行任务）。返回最近一条 running
    尝试；无活动尝试返回 None。
    """
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    row = db.query_one(
        f"""SELECT * FROM evaluation_sessions
            WHERE bound_dossier_id = ? AND status IN ({placeholders})
            ORDER BY created_at DESC LIMIT 1""",
        (dossier_id, *ACTIVE_STATUSES),
    )
    return _deserialize(row) if row else None


def list_attempts_by_dossier(dossier_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """列某证据卷宗的全部评估尝试（含失败/停止历史，需求 §41 尝试留痕）。"""
    rows = db.query_all(
        """SELECT * FROM evaluation_sessions
           WHERE bound_dossier_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (dossier_id, limit),
    )
    return [_deserialize(dict(r)) for r in rows]


def get_attempt_by_sealed_dossier(sealed_dossier_id: str) -> dict[str, Any] | None:
    """按封存的评估卷宗 id 反查评估尝试（进化绑定校验用）。"""
    row = db.query_one(
        "SELECT * FROM evaluation_sessions WHERE sealed_dossier_id = ?",
        (sealed_dossier_id,),
    )
    return _deserialize(row) if row else None


__all__ = [
    "create_session",
    "update_session",
    "get_session",
    "list_sessions",
    "get_done_by_trace",
    "list_evaluated_traces",
    "get_active_attempt_by_dossier",
    "list_attempts_by_dossier",
    "get_attempt_by_sealed_dossier",
]
