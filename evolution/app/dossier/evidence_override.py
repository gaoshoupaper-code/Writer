"""人工确认"用户主动停止但有价值"的创作 trace 进证据编纂。

REQ-20260802-211032 核心服务层。产品负责人对 cancelled+user_stop trace 发起确认后：
  1. 原子写入 runs 表正交审计列（不动 status，CON-001 终态单调性）；
  2. 立即触发 trace_payload_recovery 恢复停止前半成品为不可变 ArtifactRevision（FR-002）；
  3. 该 trace 随即满足 assess_creation_trace 来源资格，可进入证据编纂。

确认/撤回的并发与幂等：
  - 确认用 UPDATE ... WHERE evidence_override_approved=0 保证只有一个 approver 生效
    （首次确认 / 并发重复确认场景）；撤回后重新确认则覆盖 approver 取最新请求者；
  - 已有恢复产物（provenance=trace_payload_recovery）则跳过恢复（EDGE-005 幂等）；
  - 撤回仅设 revoked_at，不删已恢复产物与已编卷宗（DEC-003 审计不可逆）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import app.core.db as db

logger = logging.getLogger("evolution.dossier.evidence_override")


class EvidenceOverrideError(Exception):
    """确认/撤回业务前置校验失败。status_code 属性供 API 层映射 HTTP 状态码。"""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def approve_evidence_override(
    trace_id: str, *, approver_user_id: str, reason: str
) -> dict[str, Any]:
    """确认一条用户主动停止的 trace 有价值，立即恢复半成品产物。

    返回 {"trace_id", "approved", "approver", "recovery": {...}}。
    幂等：对已确认 trace 再次确认返回当前状态且不重复恢复（EDGE-005）。
    """
    reason = (reason or "").strip()
    if not reason:
        raise EvidenceOverrideError("确认理由不能为空")

    run = db.query_one("SELECT * FROM runs WHERE trace_id=?", (trace_id,))
    if run is None:
        raise EvidenceOverrideError("trace 未被 evolution 摄入（runs 表无记录）", status_code=404)

    _assert_approvable(run)

    # 并发安全：仅当当前未确认（approved=0）时写入，保证多个产品负责人同时确认
    # 只有一个生效（EDGE-005）。approver 取首个成功者，不闪烁。
    now = datetime.now(UTC).isoformat()
    cur = db.execute(
        """UPDATE runs
           SET evidence_override_approved=1,
               evidence_override_approver=?,
               evidence_override_reason=?,
               evidence_override_approved_at=?,
               evidence_override_revoked_at=NULL
           WHERE trace_id=? AND evidence_override_approved=0""",
        (approver_user_id, reason, now, trace_id),
    )
    already_approved = cur.rowcount == 0
    if already_approved:
        # 已确认（可能是并发或重复请求）——幂等返回当前状态，不重复恢复。
        current = db.query_one(
            """SELECT evidence_override_approver, evidence_override_revoked_at
               FROM runs WHERE trace_id=?""",
            (trace_id,),
        )
        if current and current.get("evidence_override_revoked_at"):
            # 极端：approved=1 但 revoked_at 非 NULL（已撤回后重复确认）。
            # 走完整重新确认（覆盖 approver/reason，清 revoked_at，触发恢复）。
            db.execute(
                """UPDATE runs
                   SET evidence_override_approved=1,
                       evidence_override_approver=?,
                       evidence_override_reason=?,
                       evidence_override_approved_at=?,
                       evidence_override_revoked_at=NULL
                   WHERE trace_id=?""",
                (approver_user_id, reason, now, trace_id),
            )
        recovery = _trigger_recovery(trace_id)
        # 并发场景下 current 必存在（trace 未被删）；approver 取首个成功者。
        return {
            "trace_id": trace_id,
            "approved": True,
            "approver": (current.get("evidence_override_approver") if current else None) or approver_user_id,
            "recovery": recovery,
        }

    recovery = _trigger_recovery(trace_id)
    return {
        "trace_id": trace_id,
        "approved": True,
        "approver": approver_user_id,
        "recovery": recovery,
    }


def revoke_evidence_override(trace_id: str) -> dict[str, Any]:
    """撤回人工确认。仅清退确认标记，不删已恢复产物与已编卷宗（DEC-003）。

    幂等：对未确认或已撤回的 trace 撤回返回成功且无副作用（FR-003 失败语义）。
    """
    run = db.query_one(
        "SELECT evidence_override_approved, evidence_override_revoked_at FROM runs WHERE trace_id=?",
        (trace_id,),
    )
    if run is None:
        raise EvidenceOverrideError("trace 未被 evolution 摄入", status_code=404)

    # 已撤回或从未确认 → 幂等返回，无副作用。
    if not run.get("evidence_override_approved") or run.get("evidence_override_revoked_at"):
        return {"trace_id": trace_id, "revoked": True, "noop": True}

    db.execute(
        "UPDATE runs SET evidence_override_revoked_at=? WHERE trace_id=?",
        (datetime.now(UTC).isoformat(), trace_id),
    )
    return {"trace_id": trace_id, "revoked": True, "noop": False}


def _assert_approvable(run: dict[str, Any]) -> None:
    """校验确认前置条件（FR-001/EDGE-001）：cancelled + user_stop。"""
    status = str(run.get("status") or "")
    if status != "cancelled":
        raise EvidenceOverrideError(
            f"仅用户主动停止的 trace 可确认（当前 status={status or 'unknown'}）",
            status_code=422,
        )
    cancel_audit_raw = run.get("cancel_audit")
    if not cancel_audit_raw:
        raise EvidenceOverrideError("取消原因不可判定（cancel_audit 为空）", status_code=422)
    try:
        cancel_audit = (
            json.loads(cancel_audit_raw)
            if isinstance(cancel_audit_raw, str)
            else cancel_audit_raw
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        raise EvidenceOverrideError("取消原因不可判定（cancel_audit 解析失败）") from None
    reason = cancel_audit.get("reason") if isinstance(cancel_audit, dict) else None
    if reason != "user_stop":
        raise EvidenceOverrideError(
            f"仅用户主动停止的 trace 可确认（当前取消原因={reason or 'unknown'}）",
            status_code=422,
        )


def _trigger_recovery(trace_id: str) -> dict[str, Any]:
    """触发 trace_payload_recovery，幂等检测已有产物（EDGE-005）。

    恢复全部失败时确认标记仍保留，编纂将因产物空失败（EDGE-002/EDGE-003）。
    返回恢复报告：{"recovered_count", "status", "skipped", "error"}。
    """
    # 幂等检测：已有该 source 的 trace_payload_recovery 产物则跳过恢复。
    existing = db.query_one(
        """SELECT COUNT(*) AS count
           FROM artifact_revisions
           WHERE source_trace_id=? AND provenance='trace_payload_recovery'""",
        (trace_id,),
    )
    if existing and existing["count"] > 0:
        return {
            "recovered_count": existing["count"],
            "status": "skipped",
            "skipped": True,
            "reason": "已有恢复产物，跳过重复恢复",
        }

    from app.dossier.recovery import TracePayloadRecoveryError, recover_trace_artifacts

    try:
        result = recover_trace_artifacts(
            trace_id, allow_cancelled_approved=True
        )
        return {
            "recovered_count": result.get("recovered_head_count", 0),
            "status": "completed",
            "skipped": False,
            "recovery_trace_id": result.get("recovery_trace_id"),
        }
    except TracePayloadRecoveryError as exc:
        # 恢复失败（payload 过期/hash 不匹配/无成功 write）：确认标记保留，
        # 编纂将因产物空失败。返回失败报告但不抛——产品负责人的人工判断仍被记录。
        return {
            "recovered_count": 0,
            "status": "failed",
            "skipped": False,
            "error": str(exc),
        }
    except Exception as exc:
        # 物化阶段异常（sqlite3.IntegrityError/OSError 等非 TracePayloadRecoveryError）：
        # 此时 approved 已持久化（EDGE-003），绝不穿透成 500——产品负责人需看到确认已生效
        # 但恢复失败。logger.exception 保留堆栈供排查。
        logger.exception(
            "evidence_override recovery unexpected error for trace %s", trace_id
        )
        return {
            "recovered_count": 0,
            "status": "failed",
            "skipped": False,
            "error": f"恢复过程异常：{exc}",
        }


__all__ = [
    "EvidenceOverrideError",
    "approve_evidence_override",
    "revoke_evidence_override",
]
