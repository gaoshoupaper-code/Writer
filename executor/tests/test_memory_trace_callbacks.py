"""记忆系统 trace 埋点端到端测试（REQ-20260803-120434）。

FR-004 / AC-004：覆盖**真实** ``TraceRecorder.append_event`` 写入路径，验证召回侧
（quality_callback）和写入侧（publish_callback）的 run_meta 事件都通过真实 recorder
落盘可读。该测试在未修复代码（baseline_commit）上必须失败——证明其有效性（EVD-004：
现有合成事件测试用 SimpleNamespace 绕过真实写入路径，测不出 status 漏传的 KeyError）。

修复点：
  - FR-001：``_make_quality_callback`` 的 append_event dict 必须含 ``status``（CON-001 必填）。
  - FR-002：``_make_ingestion_publish_callback`` 接线到生产/A/B 触发链，事件写进 trace run_meta。
  - FR-003：埋点失败改为可观测 warning（不再静默吞掉），但不阻断主流程（CON-002）。

设计依据：.claude/md/20260803_120434_记忆系统trace埋点不可见根治.md
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.platform.trace.recorder import TraceRecorder
from app.schemas.screenplay import ThreadSummary

# ── 加载 harness 源仓库作为 package（与 test_ab_memory_integration.py 同款做法）──
# harness 包定义 _make_quality_callback（召回侧埋点回调构造器）。
_REPO_DIR = Path(__file__).resolve().parent.parent.parent / "evolution" / "harnesses" / "repo"
_PKG_NAME = "_harness_mem_trace_pkg"
if _PKG_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        _PKG_NAME,
        _REPO_DIR / "__init__.py",
        submodule_search_locations=[str(_REPO_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[_PKG_NAME] = pkg
    spec.loader.exec_module(pkg)


def _thread(workspace: Path) -> ThreadSummary:
    return ThreadSummary(
        thread_id="thread-mem-trace", workspace_id="ws-mem", session_name="s",
        workspace_path=str(workspace), created_at="2026-08-03T00:00:00+00:00",
        updated_at="2026-08-03T00:00:00+00:00",
    )


def _run_meta_events(recorder: TraceRecorder, thread: ThreadSummary, trace_id: str) -> list:
    """读 trace jsonl，返回所有 type=run_meta 的事件（走真实落盘路径）。"""
    detail = recorder.read_run(thread, trace_id)
    assert detail is not None, "trace detail 应可读（事件已落盘）"
    return [ev for ev in detail.events if ev.type == "run_meta"]


class MemoryQualityCallbackRealRecorderTest(unittest.TestCase):
    """FR-001 / AC-001：召回侧 quality_callback 经真实 append_event 写入 trace。

    baseline 上 _make_quality_callback 漏传 status → recorder.append_event 在
    recorder.py:351 硬读 values["status"] 抛 KeyError → 被 except 吞掉 → trace 无事件。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._workspace = Path(self._tmp.name)
        self._thread = _thread(self._workspace)
        self._recorder = TraceRecorder()
        self._handle = self._recorder.create_run(self._thread, "screenplay.generate.stream")
        self._trace_id = self._handle.trace_id

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_ctx(self) -> SimpleNamespace:
        """构造最小 ctx（_make_quality_callback 只读 trace_recorder + trace_id）。"""
        return SimpleNamespace(
            trace_recorder=self._recorder,
            trace_id=self._trace_id,
        )

    def test_successful_recall_writes_memory_quality_event(self) -> None:
        """成功召回 → trace 含 source=middleware、input.memory_quality、retrieval_ok=True 事件。"""
        ctx = self._make_ctx()
        callback = pkg._make_quality_callback(ctx)
        self.assertIsNotNone(callback, "recorder+trace_id 就绪时应构造回调")

        callback({
            "chapter_num": 1,
            "query": "主角的过去",
            "retrieval_ok": True,
            "evidence_nodes_count": 3,
        })

        events = _run_meta_events(self._recorder, self._thread, self._trace_id)
        mq_events = [
            ev for ev in events
            if ev.source == "middleware" and ev.input
            and isinstance(ev.input, dict) and "memory_quality" in ev.input
        ]
        self.assertEqual(len(mq_events), 1, "应写一条 memory_quality run_meta 事件")
        self.assertTrue(mq_events[0].input["memory_quality"]["retrieval_ok"])
        self.assertEqual(mq_events[0].input["memory_quality"]["chapter_num"], 1)

    def test_failed_recall_writes_retrieval_ok_false(self) -> None:
        """EDGE-002：召回失败也写事件，retrieval_ok=False（评估器据此判 participated）。"""
        ctx = self._make_ctx()
        callback = pkg._make_quality_callback(ctx)

        callback({
            "chapter_num": 2,
            "query": "配角关系",
            "retrieval_ok": False,
            "error": "backend_unhealthy",
        })

        events = _run_meta_events(self._recorder, self._thread, self._trace_id)
        mq_events = [
            ev for ev in events
            if ev.input and isinstance(ev.input, dict) and "memory_quality" in ev.input
        ]
        self.assertEqual(len(mq_events), 1)
        self.assertFalse(mq_events[0].input["memory_quality"]["retrieval_ok"])
        self.assertEqual(
            mq_events[0].input["memory_quality"]["error"], "backend_unhealthy",
        )

    def test_none_recorder_returns_none_no_callback(self) -> None:
        """EDGE-001：recorder 为 None 时返回 None（合理降级，不埋点）。"""
        ctx = SimpleNamespace(trace_recorder=None, trace_id=self._trace_id)
        self.assertIsNone(pkg._make_quality_callback(ctx))


class IngestionPublishCallbackRealRecorderTest(unittest.TestCase):
    """FR-002 / AC-002：写入侧 publish_callback 经真实 append_event 写入 trace。

    baseline 上 _make_ingestion_publish_callback 不存在（设施建好但从未接线，EVD-003）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._workspace = Path(self._tmp.name)
        self._thread = _thread(self._workspace)
        self._recorder = TraceRecorder()
        self._handle = self._recorder.create_run(self._thread, "screenplay.generate.stream")
        self._trace_id = self._handle.trace_id

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_successful_ingestion_writes_memory_ingestion_event(self) -> None:
        """章节抽取入库成功 → trace 含 input.memory_ingestion、ok=True 事件。"""
        from app.domains.writing.events import _make_ingestion_publish_callback

        callback = _make_ingestion_publish_callback(self._recorder, self._trace_id)
        self.assertIsNotNone(callback)

        callback({
            "chapter_index": 1,
            "stats": {"scene": 3, "character_state": 2},
            "total_records": 5,
            "duration_ms": 1200,
            "ok": True,
            "error": None,
        })

        events = _run_meta_events(self._recorder, self._thread, self._trace_id)
        mi_events = [
            ev for ev in events
            if ev.input and isinstance(ev.input, dict) and "memory_ingestion" in ev.input
        ]
        self.assertEqual(len(mi_events), 1, "应写一条 memory_ingestion run_meta 事件")
        self.assertTrue(mi_events[0].input["memory_ingestion"]["ok"])
        self.assertEqual(mi_events[0].input["memory_ingestion"]["chapter_index"], 1)
        self.assertEqual(mi_events[0].input["memory_ingestion"]["total_records"], 5)

    def test_none_recorder_returns_none_no_callback(self) -> None:
        """recorder 为 None 时返回 None（向后兼容）。"""
        from app.domains.writing.events import _make_ingestion_publish_callback

        self.assertIsNone(_make_ingestion_publish_callback(None, self._trace_id))
        self.assertIsNone(_make_ingestion_publish_callback(self._recorder, None))


class TelemetryFailureIsObservableTest(unittest.TestCase):
    """FR-003 / AC-003 / AC-006：埋点写入失败时输出 warning，不静默、不阻断主流程。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._workspace = Path(self._tmp.name)
        self._thread = _thread(self._workspace)
        self._recorder = TraceRecorder()
        self._handle = self._recorder.create_run(self._thread, "screenplay.generate.stream")
        self._trace_id = self._handle.trace_id

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_quality_callback_failure_logs_warning_and_does_not_raise(self) -> None:
        """recorder.append_event 抛异常 → warning 日志 + 回调不抛（主流程不阻断）。"""
        ctx = SimpleNamespace(trace_recorder=self._recorder, trace_id=self._trace_id)
        callback = pkg._make_quality_callback(ctx)

        # 注入 recorder 异常（模拟序列化/磁盘失败，EDGE-003）。
        with (
            patch.object(self._recorder, "append_event", side_effect=RuntimeError("disk full")),
            self.assertLogs("harness_package", level="WARNING") as cm,
        ):
            callback({"chapter_num": 1, "retrieval_ok": True})  # 不应抛异常

        self.assertTrue(
            any("memory_quality 埋点写入失败" in msg for msg in cm.output),
            "应输出 warning 日志（含埋点失败原因）",
        )

    def test_ingestion_callback_failure_logs_warning_and_does_not_raise(self) -> None:
        """写入侧埋点失败同样 warning 不静默（DEC-002 两处都治）。"""
        from app.domains.writing.events import _make_ingestion_publish_callback

        callback = _make_ingestion_publish_callback(self._recorder, self._trace_id)
        with (
            patch.object(self._recorder, "append_event", side_effect=RuntimeError("seq failed")),
            self.assertLogs("app.domains.writing.events", level="WARNING") as cm,
        ):
            callback({"chapter_index": 1, "ok": True})  # 不应抛异常

        self.assertTrue(
            any("memory_ingestion 埋点写入失败" in msg for msg in cm.output),
            "应输出 warning 日志",
        )


class IngestionCallbackWiredThroughTriggerTest(unittest.TestCase):
    """FR-002 接线验证：trigger_chapter_ingestion → extract_and_publish_sync 传递 publish_callback。"""

    def test_trigger_forwards_publish_callback(self) -> None:
        """trigger_chapter_ingestion 把 publish_callback 透传到 extract_and_publish_sync。"""
        from app.domains.writing.events import trigger_chapter_ingestion

        sentinel_cb = MagicMock()
        with patch(
            "app.domains.writing.events.extract_and_publish_sync",
            return_value={},
        ) as mock_sync:
            trigger_chapter_ingestion(
                Path("/tmp/ws"), "ws-id", 3, publish_callback=sentinel_cb,
            )
            mock_sync.assert_called_once()
            self.assertIs(
                mock_sync.call_args.kwargs.get("publish_callback"), sentinel_cb,
                "publish_callback 必须透传到 extract_and_publish_sync",
            )


if __name__ == "__main__":
    unittest.main()
