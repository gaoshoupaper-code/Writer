"""历史孤儿 Trace 证据化迁移测试（FR-009, DEC-006, AC-012）。

验证 Phase 8 迁移：
  - 有取消证据（cancel_audit / 关联 test cancelled / cancel 事件）→ 补偿 cancelled。
  - 无取消证据（纯失联孤儿）→ interrupted（不伪造取消）。
  - 幂等：重复运行无副作用。
  - 已合法终态 → 不改写。
  - 不删原始数据，写迁移审计。

跑法（在 evolution 目录）：
    python -m pytest tests/test_orphan_migrate.py -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import app.core.db as db
from app.core.settings import settings
from app.ingestion.orphan_migrate import migrate_orphans, _ensure_audit_table


def _seed_run(trace_id: str, *, status: str = "running", integrity: str = "incomplete",
              phase: str | None = None, cancel_audit: str | None = None) -> None:
    db.execute(
        "INSERT INTO runs (trace_id, workspace_id, thread_id, session_name, endpoint, status, "
        "started_at, event_count, ingested_at, schema_version, integrity_status, trace_phase, cancel_audit) "
        "VALUES (?, 'ws', 'th', 's', 'ab', ?, '2026-07-29T00:00:00+00:00', 0, "
        "'2026-07-29T00:00:00+00:00', 2, ?, ?, ?)",
        (trace_id, status, integrity, phase, cancel_audit),
    )


class OrphanMigrateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = settings.evolution_db
        self.old_payload = settings.trace_payload_dir
        settings.evolution_db = str(Path(self.tmp.name) / "evolution.db")
        settings.trace_payload_dir = str(Path(self.tmp.name) / "payloads")
        db._conn = None
        db.init_db()
        _ensure_audit_table()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        settings.evolution_db = self.old_db
        settings.trace_payload_dir = self.old_payload
        self.tmp.cleanup()

    def test_orphan_with_cancel_audit_converges_to_cancelled(self) -> None:
        """DEC-006：有 cancel_audit 证据 → 补偿 cancelled。"""
        audit = json.dumps({"cancel_id": "c1", "requested_by": "user"})
        _seed_run("trace-audit", cancel_audit=audit)
        result = migrate_orphans()
        self.assertEqual(result["migrated_to_cancelled"], 1)
        row = db.query_one("SELECT status, trace_phase FROM runs WHERE trace_id='trace-audit'")
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["trace_phase"], "degraded")

    def test_orphan_with_cancelled_test_converges_to_cancelled(self) -> None:
        """关联 manual_tests 已 cancelled → 补偿 cancelled。"""
        _seed_run("trace-test")
        db.execute(
            "INSERT INTO manual_tests (test_id, case_id, version_type, trace_id, status, created_at) "
            "VALUES ('t1', 'case', 'working', 'trace-test', 'cancelled', '2026-07-29')",
        )
        result = migrate_orphans()
        self.assertEqual(result["migrated_to_cancelled"], 1)

    def test_orphan_without_evidence_marked_interrupted(self) -> None:
        """CON-004：无取消证据的失联孤儿 → interrupted（不伪造取消）。"""
        _seed_run("trace-lost")
        result = migrate_orphans()
        self.assertEqual(result["marked_interrupted"], 1)
        self.assertEqual(result["migrated_to_cancelled"], 0)
        row = db.query_one("SELECT status, integrity_status FROM runs WHERE trace_id='trace-lost'")
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["integrity_status"], "incomplete")  # 不伪造 verified

    def test_terminal_runs_not_migrated(self) -> None:
        """已合法终态（completed/failed）→ 不改写（RSK-003 不误改合法终态）。"""
        _seed_run("trace-done", status="completed", integrity="verified", phase="sealed")
        _seed_run("trace-fail", status="failed", integrity="incomplete", phase="degraded")
        result = migrate_orphans()
        self.assertEqual(result["scanned"], 0)  # running/awaiting/cancelling 之外的不扫
        self.assertEqual(result["migrated_to_cancelled"], 0)

    def test_migration_is_idempotent(self) -> None:
        """幂等：重复运行无副作用（AC-012 重复执行）。

        第一次迁移后 trace 已收敛为 interrupted（不再是 orphan 状态），第二次扫描
        不会重复发现它，也不会重复写审计。零副作用 = 不重复迁移、不重复写审计。
        """
        _seed_run("trace-idem")
        result1 = migrate_orphans()
        self.assertEqual(result1["marked_interrupted"], 1)
        # 第二次运行：trace 已是 interrupted（不在 orphan 扫描集合），不会被重复处理。
        result2 = migrate_orphans()
        self.assertEqual(result2["scanned"], 0)
        self.assertEqual(result2["marked_interrupted"], 0)  # 不重复迁移
        # 审计表只有一条记录（无重复）。
        audits = db.query_all("SELECT * FROM orphan_migrations WHERE trace_id='trace-idem'")
        self.assertEqual(len(audits), 1)

    def test_migration_writes_audit_record(self) -> None:
        """迁移审计：orphan_migrations 表记录前后状态 + 原因（可审计/可回滚）。"""
        _seed_run("trace-aud")
        migrate_orphans()
        audit = db.query_one("SELECT * FROM orphan_migrations WHERE trace_id='trace-aud'")
        self.assertIsNotNone(audit)
        self.assertEqual(audit["prior_status"], "running")
        self.assertEqual(audit["target_status"], "interrupted")
        self.assertIn("no_surviving_owner", audit["reason"])

    def test_dry_run_does_not_write(self) -> None:
        """dry_run=True 只报告不写库（运维预演）。"""
        _seed_run("trace-dry")
        result = migrate_orphans(dry_run=True)
        self.assertEqual(result["scanned"], 1)
        row = db.query_one("SELECT status FROM runs WHERE trace_id='trace-dry'")
        self.assertEqual(row["status"], "running")  # 未改动


if __name__ == "__main__":
    unittest.main()
