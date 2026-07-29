"""Trace 完整性结构化诊断测试（FR-003 / AC-004）。

验证 _compute_integrity_diagnosis 对各类完整性状态给出具体失败检查、影响下游
和恢复动作，而非"Trace 数据不完整：不完整"这类同义反复。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import app.core.db as db
from app.core.settings import settings
from app.view.traces import _compute_integrity_diagnosis


class IntegrityDiagnosisTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = settings.evolution_db
        self.old_payload_dir = settings.trace_payload_dir
        settings.evolution_db = str(Path(self.tmp.name) / "evolution.db")
        settings.trace_payload_dir = str(Path(self.tmp.name) / "payloads")
        db._conn = None
        db.init_db()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        settings.evolution_db = self.old_db
        settings.trace_payload_dir = self.old_payload_dir
        self.tmp.cleanup()

    def _insert_run(
        self, trace_id: str, *, integrity: str, schema_version: int = 2,
        phase: str | None = None,
    ) -> None:
        db.execute(
            """INSERT INTO runs
               (trace_id, workspace_id, thread_id, session_name, endpoint, status,
                started_at, event_count, ingested_at, schema_version, service, workload,
                integrity_status, trace_phase)
               VALUES (?, 'ws', 'thread', 'session', 'create', 'completed',
                       '2026-01-01T00:00:00+00:00', 1, '2026-01-01T00:00:01+00:00',
                       ?, 'executor', 'creation', ?, ?)""",
            (trace_id, schema_version, integrity, phase),
        )

    def _insert_receipt(
        self, trace_id: str, *, manifest_status: str = "verified",
        capture_degraded: bool = False, missing_ranges: list | None = None,
        manifest_present: bool = True,
    ) -> None:
        manifest_json = None
        if manifest_present:
            manifest_json = json.dumps({
                "trace_id": trace_id, "final_sequence": 10,
                "terminal_event_id": "evt-10", "events_hash": "abc",
                "payload_ids": [], "capture_degraded": capture_degraded,
                "created_at": "2026-01-01T00:00:01+00:00",
            })
        db.execute(
            """INSERT INTO trace_receipts
               (trace_id, contiguous_seq, max_seen_seq, missing_ranges_json,
                manifest_json, manifest_status, receipt_revision, updated_at)
               VALUES (?, 10, 10, ?, ?, ?, 1, '2026-01-01T00:00:01+00:00')""",
            (trace_id, json.dumps(missing_ranges or []), manifest_json, manifest_status),
        )

    def test_verified_trace_has_no_missing_checks(self) -> None:
        """verified Trace 返回空 missing_checks（前端用同一套渲染逻辑）。"""
        self._insert_run("trace-ok", integrity="verified")
        diag = _compute_integrity_diagnosis("trace-ok")
        self.assertEqual(diag.integrity_status, "verified")
        self.assertEqual(diag.missing_checks, [])
        self.assertEqual(diag.affected_downstreams, [])

    def test_pending_phase_returns_neutral_not_fault(self) -> None:
        """FR-008/AC-010：pending（记录/封存中）返回中性诊断，不当终态 incomplete 故障。

        EVD-005 根因：运行中 trace 完整性是 pending，UI 不得显示"数据损坏/不可消费"。
        """
        self._insert_run("trace-rec", integrity="pending", phase="recording", schema_version=2)
        diag = _compute_integrity_diagnosis("trace-rec")
        self.assertEqual(diag.integrity_status, "pending")
        self.assertEqual(len(diag.missing_checks), 1)
        self.assertEqual(diag.missing_checks[0].check, "integrity_pending")
        # pending 不算缺口——下游只是暂时等待，不应显示"不可消费"。
        self.assertEqual(diag.affected_downstreams, [])
        # 文案明确告知"这不是数据损坏"。
        self.assertIn("不是数据损坏", diag.missing_checks[0].impact)

    def test_pending_sealing_phase_label_correct(self) -> None:
        """sealing 阶段的 pending 诊断显示"封存中"文案。"""
        self._insert_run("trace-seal", integrity="pending", phase="sealing", schema_version=2)
        diag = _compute_integrity_diagnosis("trace-seal")
        self.assertIn("封存中", diag.missing_checks[0].impact)

    def test_incomplete_with_capture_degraded_gives_specific_check(self) -> None:
        """AC-004：capture_degraded 导致的 incomplete 必须给出具体缺口，非同义反复。"""
        self._insert_run("trace-degraded", integrity="incomplete")
        self._insert_receipt("trace-degraded", capture_degraded=True)
        diag = _compute_integrity_diagnosis("trace-degraded")
        self.assertEqual(diag.integrity_status, "incomplete")
        self.assertTrue(len(diag.missing_checks) >= 1)
        # 必须含 payload_capture_degraded 检查
        checks = [c.check for c in diag.missing_checks]
        self.assertIn("payload_capture_degraded", checks)
        # 每项必须有影响 + 恢复动作（不能只给标签）
        for check in diag.missing_checks:
            self.assertTrue(check.impact)
            self.assertTrue(check.recovery)
        # 受影响下游必须列出
        self.assertIn("evaluation", diag.affected_downstreams)

    def test_incomplete_with_sequence_gap_gives_specific_check(self) -> None:
        """事件序列缺口必须诊断出具体缺口数。"""
        self._insert_run("trace-gap", integrity="incomplete")
        self._insert_receipt(
            "trace-gap", missing_ranges=[[3, 5], [8, 9]]
        )
        diag = _compute_integrity_diagnosis("trace-gap")
        checks = [c.check for c in diag.missing_checks]
        self.assertIn("event_sequence_gap", checks)
        gap_check = next(c for c in diag.missing_checks if c.check == "event_sequence_gap")
        self.assertIn("2", gap_check.impact)  # 两处缺口

    def test_legacy_trace_gives_legacy_check_and_recovery(self) -> None:
        """DEC-006 / EDGE-005：旧版 Trace 标记未验证 + 提供重跑动作。"""
        self._insert_run("trace-old", integrity="legacy", schema_version=1)
        diag = _compute_integrity_diagnosis("trace-old")
        self.assertEqual(diag.integrity_status, "legacy")
        self.assertEqual(len(diag.missing_checks), 1)
        self.assertEqual(diag.missing_checks[0].check, "legacy_trace_unverified")
        self.assertIn("重新运行", diag.missing_checks[0].recovery)

    def test_missing_manifest_gives_specific_check(self) -> None:
        """缺少终态清单必须诊断出，不能只说"不完整"。"""
        self._insert_run("trace-nomanifest", integrity="incomplete")
        self._insert_receipt(
            "trace-nomanifest", manifest_status="missing", manifest_present=False
        )
        diag = _compute_integrity_diagnosis("trace-nomanifest")
        checks = [c.check for c in diag.missing_checks]
        self.assertIn("manifest_missing", checks)

    def test_not_found_raises_404(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            _compute_integrity_diagnosis("nonexistent")
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
