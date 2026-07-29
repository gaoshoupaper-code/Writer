"""兜底扫描三方对账测试（EVD-009, FR-005）。

验证 Phase 3 根因修复：孤儿 trace（executor+evolution 两端都 running，但关联
manual_tests 已终态）不再被永久跳过，而是强制重拉摄入让 receipt 按真实事件判定。

线上样本（EVD-006 trace-02c7659e…）：测试 cancelled、executor/evolution trace 永久
running/incomplete——旧扫描因两端 status 相同而跳过，孤儿永驻。

跑法（在 evolution 目录）：
    python -m pytest tests/test_orphan_scan_reconcile.py -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.core.db as db
from app.core.settings import settings
from app.ingestion import scan
from contracts.trace import TraceLogEvent, TraceRunSummary


TRACE_ID = "trace-orphan"


def _event(sequence: int, event_type: str) -> TraceLogEvent:
    return TraceLogEvent(
        trace_id=TRACE_ID,
        event_id=f"event-{sequence}",
        sequence=sequence,
        type=event_type,
        status="running",
        timestamp=f"2026-07-29T05:32:4{sequence}+00:00",
        source="runtime",
        schema_version=2,
    )


def _run(status: str = "running") -> TraceRunSummary:
    return TraceRunSummary(
        trace_id=TRACE_ID,
        workspace_id="ab-workspace",
        thread_id="ab-thread",
        session_name="evolve-ab",
        workspace_path="",
        endpoint="screenplay.ab_run",
        status=status,
        started_at="2026-07-29T05:32:41+00:00",
        event_count=2,
        path="",
        schema_version=2,
        service="executor",
        workload="creation",
        purpose="evolution",
        integrity_status="pending",
        trace_phase="recording",
    )


class OrphanScanReconcileTest(unittest.TestCase):
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

    def _seed_running_trace(self) -> None:
        """预置一个两端都 running 的 trace（孤儿场景）。"""
        db.execute(
            "INSERT INTO runs (trace_id, workspace_id, thread_id, session_name, endpoint, "
            "status, started_at, event_count, integrity_status, schema_version, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (TRACE_ID, "ab-workspace", "ab-thread", "evolve-ab", "screenplay.ab_run",
             "running", "2026-07-29T05:32:41+00:00", 2, "pending", 2,
             "2026-07-29T05:32:41+00:00"),
        )

    def _seed_terminal_test(self, status: str = "cancelled") -> None:
        """预置一个已终态的 manual_tests 关联该 trace（三状态分裂）。"""
        db.execute(
            "INSERT INTO manual_tests (test_id, case_id, version_type, trace_id, task_id, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test-orphan", "case-1", "working", TRACE_ID, "task-orphan", status,
             "2026-07-29T05:32:41+00:00"),
        )

    def test_associated_business_terminal_detects_cancelled_test(self) -> None:
        """EVD-009：trace 两端 running 但关联 test 已 cancelled → 检测为孤儿。"""
        self._seed_running_trace()
        self._seed_terminal_test("cancelled")
        self.assertTrue(scan._associated_business_terminal(TRACE_ID))

    def test_associated_business_terminal_false_when_test_still_running(self) -> None:
        """关联 test 仍 running（真活跃）→ 不是孤儿，不强制重拉。"""
        self._seed_running_trace()
        self._seed_terminal_test("running")
        self.assertFalse(scan._associated_business_terminal(TRACE_ID))

    def test_associated_business_terminal_false_when_no_association(self) -> None:
        """无关联业务对象 → 不是孤儿（纯创作 trace）。"""
        self._seed_running_trace()
        self.assertFalse(scan._associated_business_terminal(TRACE_ID))

    def test_scan_force_reingests_orphan_with_same_status(self) -> None:
        """EVD-009 核心修复：两端都 running 但 test 终态 → 扫描强制重拉摄入。"""
        self._seed_running_trace()
        self._seed_terminal_test("cancelled")

        # executor 列表返回该 trace 为 running（与本地相同——旧代码会跳过）。
        fake_resp = SimpleNamespace()
        fake_resp.raise_for_status = lambda: None
        fake_resp.json = lambda: {"traces": [{"trace_id": TRACE_ID, "status": "running", "workspace_id": "ab-workspace"}]}

        ingested = {"called": False}
        def fake_fetch_ingest(trace_id, workspace_hint):
            ingested["called"] = True
            return trace_id

        with patch("httpx.get", return_value=fake_resp), \
             patch("app.ingestion.scan._fetch_and_ingest", side_effect=fake_fetch_ingest):
            count = scan._scan_once()

        self.assertTrue(ingested["called"], "孤儿 trace 应被强制重拉摄入")
        self.assertEqual(count, 1)

    def test_scan_skips_genuinely_active_trace(self) -> None:
        """真活跃 trace（两端 running + test 也 running）→ 跳过，不重拉。"""
        self._seed_running_trace()
        self._seed_terminal_test("running")

        fake_resp = SimpleNamespace()
        fake_resp.raise_for_status = lambda: None
        fake_resp.json = lambda: {"traces": [{"trace_id": TRACE_ID, "status": "running", "workspace_id": "ab-workspace"}]}

        ingested = {"called": False}
        def fake_fetch_ingest(trace_id, workspace_hint):
            ingested["called"] = True
            return trace_id

        with patch("httpx.get", return_value=fake_resp), \
             patch("app.ingestion.scan._fetch_and_ingest", side_effect=fake_fetch_ingest):
            scan._scan_once()

        self.assertFalse(ingested["called"], "真活跃 trace 不应被重拉")


if __name__ == "__main__":
    unittest.main()
