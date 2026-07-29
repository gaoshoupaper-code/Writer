"""单次测试跨服务 trace 关联与可用性契约（FR-003 / FR-004 / DEC-005）。

覆盖：
  - _trigger_executor 透传 traceparent / test_id 到 executor /internal/ab/run（FR-004）
  - _trace_availability 判定 preparing / available / unavailable / none（FR-003 / DEC-003）

跑法（在 evolution 目录）：
    python -m pytest tests/test_ab_trace_propagation_contract.py -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# 占位 executor URL，避免真实连接（与 test_trace_v2_ingestion 约定一致）。
os.environ.setdefault("EXECUTOR_URL", "http://127.0.0.1:0")

import app.core.db as db
from app.core.settings import settings
from app.tests.api import _trigger_executor, _trace_availability


def _make_test_row(*, trace_id, status) -> dict:
    """构造一个 manual_tests 行字典（_trace_availability 只读 status / trace_id）。"""
    return {
        "test_id": "t1",
        "trace_id": trace_id,
        "status": status,
    }


class TriggerExecutorPropagationTest(unittest.TestCase):
    """FR-004：_trigger_executor 必须透传 test_id 与 traceparent。"""

    def test_payload_includes_test_id_and_traceparent(self) -> None:
        captured: dict = {}

        class FakeResp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"task_id": "task-xyz"}

        def fake_post(url, json=None, timeout=None):  # noqa: ARG001
            captured["url"] = url
            captured["payload"] = json
            return FakeResp()

        with patch("app.tests.api.httpx.post", side_effect=fake_post):
            task_id = _trigger_executor(
                demand_md="# 需求",
                version_type="working",
                snapshot=None,
                test_id="test-abc",
                traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            )

        self.assertEqual(task_id, "task-xyz")
        payload = captured["payload"]
        self.assertEqual(payload["test_id"], "test-abc")
        self.assertTrue(payload["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-"))
        self.assertEqual(payload["demand_md"], "# 需求")
        self.assertTrue(payload["baseline"])  # working

    def test_payload_without_traceparent_omits_field(self) -> None:
        """兼容：未传 traceparent 时不下发该字段（executor 自行生成）。"""
        captured: dict = {}

        class FakeResp:
            def raise_for_status(self): return None

            def json(self): return {"task_id": "t"}

        with patch("app.tests.api.httpx.post",
                   side_effect=lambda url, json=None, timeout=None: captured.update(payload=json) or FakeResp()):
            _trigger_executor(
                demand_md="d", version_type="snapshot",
                snapshot={"source_commit": "abc"}, test_id="t1",
            )
        self.assertNotIn("traceparent", captured["payload"])
        self.assertEqual(captured["payload"]["test_id"], "t1")
        self.assertFalse(captured["payload"]["baseline"])  # snapshot


class TraceAvailabilityTest(unittest.TestCase):
    """FR-003：_trace_availability 判定四态（依赖 runs 表是否摄入）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_db = settings.evolution_db
        settings.evolution_db = str(Path(self.tmp.name) / "evo.db")
        db._conn = None
        db.init_db()

    def tearDown(self) -> None:
        # 先关闭 SQLite 连接再清理临时目录（Windows 文件锁）。
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        settings.evolution_db = self._orig_db
        self.tmp.cleanup()

    def _insert_run(self, trace_id: str) -> None:
        """在 runs 表插入一行，模拟 evolution 已摄入该 trace。"""
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO runs (trace_id, workspace_id, status, started_at, ingested_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (trace_id, "ws-1", "running", "2026-07-29T00:00:00+00:00", "2026-07-29T00:00:00+00:00"),
            )
            conn.commit()

    def test_none_when_no_trace_id(self) -> None:
        self.assertEqual(_trace_availability(_make_test_row(trace_id=None, status="running")), "none")

    def test_available_when_ingested(self) -> None:
        self._insert_run("trace-1")
        self.assertEqual(
            _trace_availability(_make_test_row(trace_id="trace-1", status="running")),
            "available",
        )

    def test_preparing_when_running_not_yet_ingested(self) -> None:
        """EDGE-001：running 未摄入 → 准备态（吸收竞态，不暴露 not found）。"""
        self.assertEqual(
            _trace_availability(_make_test_row(trace_id="trace-2", status="running")),
            "preparing",
        )

    def test_unavailable_when_terminal_not_ingested(self) -> None:
        """DEC-003：终态有 trace_id 但 runs 查无 → 断链旧记录，不可恢复。"""
        for status in ("done", "failed", "cancelled", "cancel_timeout"):
            with self.subTest(status=status):
                self.assertEqual(
                    _trace_availability(_make_test_row(trace_id="trace-3", status=status)),
                    "unavailable",
                )


if __name__ == "__main__":
    unittest.main()
