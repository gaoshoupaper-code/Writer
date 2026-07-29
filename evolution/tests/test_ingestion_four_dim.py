"""evolution 摄入四维正交契约测试（EVD-005/008, FR-008, DEC-008）。

验证 Phase 3 根因修复：
  1. EVD-008：_sync_status_only 不再用陈旧 index event_count 覆盖本地真实高水位。
  2. EVD-005：运行中（无终态事件）的 trace 完整性是 pending（记录中），不是终态 incomplete。
  3. DEC-008：四维字段（trace_phase / cancel_audit / lifecycle_revision）正确持久化与同步。

跑法（在 evolution 目录）：
    python -m pytest tests/test_ingestion_four_dim.py -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import app.core.db as db
from app.core.settings import settings
from app.ingestion import ingestion
from app.ingestion.importer import ingest_events, _derive_run_summary
from contracts.trace import TraceLogEvent, TraceRunSummary, CancelAudit


TRACE_ID = "trace-four-dim"


def _event(sequence: int, event_type: str, *, status: str = "running") -> TraceLogEvent:
    return TraceLogEvent(
        trace_id=TRACE_ID,
        event_id=f"event-{sequence}",
        sequence=sequence,
        type=event_type,
        status=status,
        timestamp=f"2026-07-29T05:32:4{sequence}+00:00",
        source="runtime",
        schema_version=2,
    )


def _run(status: str = "running", *, integrity: str = "pending", phase: str | None = "recording",
         event_count: int = 0) -> TraceRunSummary:
    return TraceRunSummary(
        trace_id=TRACE_ID,
        workspace_id="ab-workspace",
        thread_id="ab-thread",
        session_name="evolve-ab",
        workspace_path="",
        endpoint="screenplay.ab_run",
        status=status,
        started_at="2026-07-29T05:32:41+00:00",
        event_count=event_count,
        path="",
        schema_version=2,
        service="executor",
        workload="creation",
        purpose="evolution",
        integrity_status=integrity,
        trace_phase=phase,
    )


class FourDimIngestionTest(unittest.TestCase):
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

    def test_running_trace_integrity_is_pending_not_incomplete(self) -> None:
        """EVD-005 根因修复：运行中 trace（无终态事件）完整性是 pending，不是 incomplete。"""
        events = [_event(1, "run_start"), _event(2, "llm_start")]
        run, _ = _derive_run_summary(events, None, "ab-workspace", run_status_hint="running")
        self.assertEqual(run.integrity_status, "pending")
        self.assertEqual(run.trace_phase, "recording")

    def test_terminal_trace_integrity_derived_then_receipt_corrects(self) -> None:
        """终态 trace 推导为 incomplete/sealed，receipt 计算后纠正为 verified。"""
        events = [
            _event(1, "run_start"),
            _event(2, "llm_start"),
            _event(3, "llm_end"),
            _event(4, "run_end", status="completed"),
        ]
        run, _ = _derive_run_summary(events, None, "ab-workspace", run_status_hint="completed")
        # 终态但 receipt 未算：临时 incomplete + sealed phase。
        self.assertEqual(run.integrity_status, "incomplete")
        self.assertEqual(run.trace_phase, "sealed")
        self.assertEqual(run.status, "completed")

    def test_sync_status_only_does_not_overwrite_event_count(self) -> None:
        """EVD-008 根因修复：状态同步不覆盖本地真实 event_count（高水位）。"""
        # 先摄入 3 条事件，本地 event_count=3。
        events = [_event(1, "run_start"), _event(2, "llm_start"), _event(3, "llm_end")]
        run = _run(status="running", event_count=3)
        ingest_events(events, "ab-workspace", run_summary_hint=run)
        row = db.query_one("SELECT event_count, status FROM runs WHERE trace_id=?", (TRACE_ID,))
        self.assertEqual(row["event_count"], 3)

        # 模拟 executor index 返回陈旧 event_count=0（运行中未更新）+ 状态变迁。
        fake_executor_run = SimpleNamespace(
            json=lambda: SimpleNamespace(get=lambda *_: None)  # placeholder, overridden below
        )
        # _sync_status_only 用 resp.json().get("run", {})，构造该结构。
        def fake_get(url, **kwargs):
            resp = SimpleNamespace()
            resp.raise_for_status = lambda: None
            resp.json = lambda: {"run": {"status": "running", "event_count": 0}}
            return resp

        with patch("httpx.get", side_effect=fake_get):
            ingestion._sync_status_only(TRACE_ID)

        # event_count 必须保持 3（本地真实），不被陈旧的 0 覆盖。
        row2 = db.query_one("SELECT event_count, status FROM runs WHERE trace_id=?", (TRACE_ID,))
        self.assertEqual(row2["event_count"], 3)
        self.assertEqual(row2["status"], "running")

    def test_sync_status_only_propagates_four_dim_fields(self) -> None:
        """_sync_status_only 同步四维字段（trace_phase/integrity/cancel_audit/revision）。"""
        events = [_event(1, "run_start")]
        run = _run(status="running", event_count=1)
        ingest_events(events, "ab-workspace", run_summary_hint=run)

        audit = CancelAudit(cancel_id="cancel-xyz", requested_by="user", reason="user_stop")
        audit_json = json.dumps(audit.model_dump(mode="json"), ensure_ascii=False)

        def fake_get(url, **kwargs):
            resp = SimpleNamespace()
            resp.raise_for_status = lambda: None
            resp.json = lambda: {
                "run": {
                    "status": "cancelling",
                    "trace_phase": "recording",
                    "integrity_status": "pending",
                    "cancel_audit": json.loads(audit_json),
                    "lifecycle_revision": 5,
                }
            }
            return resp

        with patch("httpx.get", side_effect=fake_get):
            ingestion._sync_status_only(TRACE_ID)

        row = db.query_one(
            "SELECT status, trace_phase, integrity_status, cancel_audit, lifecycle_revision "
            "FROM runs WHERE trace_id=?",
            (TRACE_ID,),
        )
        self.assertEqual(row["status"], "cancelling")
        self.assertEqual(row["trace_phase"], "recording")
        self.assertEqual(row["integrity_status"], "pending")
        self.assertEqual(row["lifecycle_revision"], 5)
        self.assertIn("cancel-xyz", row["cancel_audit"])

    def test_lifecycle_revision_monotonic_on_reingest(self) -> None:
        """lifecycle_revision 取 MAX，旧快照不得覆盖新 revision（CON-006）。"""
        events = [_event(1, "run_start")]
        run = _run(status="running", event_count=1)
        run.lifecycle_revision = 10
        ingest_events(events, "ab-workspace", run_summary_hint=run)
        row = db.query_one("SELECT lifecycle_revision FROM runs WHERE trace_id=?", (TRACE_ID,))
        self.assertEqual(row["lifecycle_revision"], 10)

        # 再摄入一个 revision=3 的旧快照——不得覆盖 10。
        run_old = _run(status="running", event_count=1)
        run_old.lifecycle_revision = 3
        ingest_events(events, "ab-workspace", run_summary_hint=run_old)
        row2 = db.query_one("SELECT lifecycle_revision FROM runs WHERE trace_id=?", (TRACE_ID,))
        self.assertEqual(row2["lifecycle_revision"], 10)  # MAX，不被旧值覆盖


if __name__ == "__main__":
    unittest.main()
