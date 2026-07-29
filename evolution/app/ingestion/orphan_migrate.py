"""历史孤儿 Trace 证据化迁移（FR-009, DEC-006, CON-004, EDGE-006）。

升级前永久停在 running/incomplete 的 Trace（线上样本 EVD-006/EVD-012）是无存活 owner
的孤儿——task/test/trace 三状态分裂的产物。本模块证据化扫描并诚实收敛：
  - 证据足以证明用户取消且能安全封存 → 补偿收敛为 cancelled（trace_phase=degraded）。
  - 证据不足（无取消记录、无终态、无法证明 owner 存活）→ 标记 interrupted/incomplete
    并给出具体诊断，保留原始文件，不伪造 verified。
  - 已合法封存的历史终态 → 不改写。

幂等：重复运行无副作用（按 trace_id + 已迁移标记去重）。
可审计：每条变更记录迁移原因、前后状态、迁移身份到 orphan_migrations 表。
可回滚：不删除原始 trace 事件/receipt/manifest，只新增审计记录 + 更新 runs 投影。

调用方式：
  from app.ingestion.orphan_migrate import migrate_orphans
  result = migrate_orphans()  # 启动恢复或运维手动触发
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import app.core.db as db

logger = logging.getLogger("evolution.orphan_migrate")

# 孤儿判定：runs.status 持续为这些"活跃/未决"态，但实际已无存活 owner。
_ORPHAN_RUN_STATUSES = {"running", "awaiting_input", "cancelling"}


def migrate_orphans(*, dry_run: bool = False) -> dict[str, Any]:
    """扫描并证据化迁移全部历史孤儿 Trace（DEC-006）。

    返回汇总：{scanned, migrated_to_cancelled, marked_interrupted, skipped_terminal,
              already_migrated, errors}。
    dry_run=True 时只报告不写库（运维预演用）。
    """
    _ensure_audit_table()
    result = {
        "scanned": 0, "migrated_to_cancelled": 0, "marked_interrupted": 0,
        "skipped_terminal": 0, "already_migrated": 0, "errors": 0,
    }
    orphans = db.query_all(
        "SELECT trace_id, status, integrity_status, trace_phase, schema_version, "
        "started_at, event_count FROM runs WHERE status IN ({})".format(
            ",".join("?" * len(_ORPHAN_RUN_STATUSES))
        ),
        tuple(_ORPHAN_RUN_STATUSES),
    )
    for row in orphans:
        result["scanned"] += 1
        trace_id = row["trace_id"]
        try:
            # 已迁移过（幂等）：跳过。
            if _is_already_migrated(trace_id):
                result["already_migrated"] += 1
                continue
            decision = _classify_orphan(trace_id, row)
            if dry_run:
                logger.info("[dry-run] %s → %s (%s)", trace_id, decision["target_status"], decision["reason"])
                continue
            _apply_migration(trace_id, decision)
            if decision["target_status"] == "cancelled":
                result["migrated_to_cancelled"] += 1
            else:
                result["marked_interrupted"] += 1
            logger.info("孤儿迁移: %s → %s (%s)", trace_id, decision["target_status"], decision["reason"])
        except Exception:
            result["errors"] += 1
            logger.exception("孤儿迁移失败: %s", trace_id)
    return result


def _classify_orphan(trace_id: str, row: Any) -> dict[str, Any]:
    """证据化判定一条孤儿的收敛目标（cancelled vs interrupted）。

    DEC-006 证据标准：
      - 可证明取消（有 cancel_audit / cancel_requested 事件 / 关联 test 已 cancelled）→ cancelled。
      - 否则（无取消证据，只是失联/重启孤儿）→ interrupted（诚实，不伪造取消）。
    """
    # 证据1：runs.cancel_audit 已持久化（Phase 2 父进程接管写过）。
    full_row = db.query_one("SELECT cancel_audit FROM runs WHERE trace_id=?", (trace_id,))
    cancel_audit = full_row.get("cancel_audit") if full_row else None
    if cancel_audit:
        return {
            "target_status": "cancelled",
            "trace_phase": "degraded",
            "integrity_status": "incomplete",
            "reason": "evidence:cancel_audit_present",
            "evidence": {"cancel_audit": cancel_audit},
        }

    # 证据2：关联 manual_tests 已 cancelled（用户停止过）。
    test_row = db.query_one("SELECT status FROM manual_tests WHERE trace_id=?", (trace_id,))
    if test_row and test_row["status"] == "cancelled":
        return {
            "target_status": "cancelled",
            "trace_phase": "degraded",
            "integrity_status": "incomplete",
            "reason": "evidence:associated_test_cancelled",
            "evidence": {"test_status": "cancelled"},
        }

    # 证据3：事件流含 cancel_requested / run_cancelled（父进程接管或子进程写过）。
    cancel_event = db.query_one(
        "SELECT 1 FROM event_payloads WHERE trace_id=? AND type IN ('cancel_requested','run_cancelled') LIMIT 1",
        (trace_id,),
    )
    if cancel_event:
        return {
            "target_status": "cancelled",
            "trace_phase": "degraded",
            "integrity_status": "incomplete",
            "reason": "evidence:cancel_event_in_stream",
            "evidence": {"has_cancel_event": True},
        }

    # 证据不足：无法证明是用户取消——诚实标记 interrupted（CON-004 不伪装活跃）。
    return {
        "target_status": "interrupted",
        "trace_phase": "degraded",
        "integrity_status": "incomplete",
        "reason": "no_surviving_owner:no_cancel_evidence",
        "evidence": {"schema_version": row["schema_version"], "started_at": row["started_at"]},
    }


def _apply_migration(trace_id: str, decision: dict[str, Any]) -> None:
    """应用迁移：更新 runs 投影 + 写迁移审计（不删原始数据）。"""
    now = datetime.now(UTC).isoformat()
    prior = db.query_one("SELECT status, integrity_status, trace_phase FROM runs WHERE trace_id=?", (trace_id,))
    db.execute(
        "UPDATE runs SET status=?, integrity_status=?, trace_phase=?, ended_at=COALESCE(ended_at,?), "
        "lifecycle_revision=lifecycle_revision+1 WHERE trace_id=?",
        (decision["target_status"], decision["integrity_status"], decision["trace_phase"], now, trace_id),
    )
    db.execute(
        """INSERT INTO orphan_migrations
           (trace_id, prior_status, prior_integrity, prior_phase, target_status, reason,
            evidence_json, migrated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (trace_id, prior["status"] if prior else None,
         prior["integrity_status"] if prior else None,
         prior["trace_phase"] if prior else None,
         decision["target_status"], decision["reason"],
         json.dumps(decision["evidence"], ensure_ascii=False), now),
    )


def _is_already_migrated(trace_id: str) -> bool:
    """幂等：该 trace 已有迁移审计记录则跳过（重复运行无副作用）。"""
    row = db.query_one(
        "SELECT 1 FROM orphan_migrations WHERE trace_id=? LIMIT 1", (trace_id,)
    )
    return row is not None


def _ensure_audit_table() -> None:
    """建迁移审计表（幂等，CON-009 兼容扩展，旧库无此表时自动建）。"""
    db.execute(
        """CREATE TABLE IF NOT EXISTS orphan_migrations (
            trace_id        TEXT PRIMARY KEY,
            prior_status    TEXT,
            prior_integrity TEXT,
            prior_phase     TEXT,
            target_status   TEXT NOT NULL,
            reason          TEXT NOT NULL,
            evidence_json   TEXT NOT NULL DEFAULT '{}',
            migrated_at     TEXT NOT NULL
        )"""
    )
