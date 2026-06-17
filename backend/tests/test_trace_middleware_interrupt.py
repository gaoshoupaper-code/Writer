"""TraceMiddleware 对 GraphInterrupt（HITL）的放行测试。

ask_user 工具通过 interrupt() 抛 GraphInterrupt 暂停图，是 HITL 的正常控制流。
TraceMiddleware 不应把它当成 tool_error / llm_error 记录，否则监测面板会误报。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from langgraph.errors import GraphInterrupt

from app.schemas.screenplay import ThreadSummary
from app.writer.middleware.trace_middleware import TraceMiddleware
from app.writer.trace.recorder import TraceRecorder


def _make_thread(workspace: Path) -> ThreadSummary:
    return ThreadSummary(
        thread_id="thread-test",
        workspace_id="workspace-test",
        session_name="session",
        workspace_path=str(workspace),
        created_at="2026-05-22T00:00:00+00:00",
        updated_at="2026-05-22T00:00:00+00:00",
    )


def _error_events(recorder: TraceRecorder, thread: ThreadSummary, trace_id: str) -> list[str]:
    """读取该 trace 的所有 error 事件类型。"""
    detail = recorder.read_run(thread, trace_id)
    assert detail is not None
    return [event.type for event in detail.events if event.type.endswith("_error")]


class TraceMiddlewareInterruptTest(unittest.TestCase):
    def test_tool_call_graph_interrupt_records_no_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            thread = _make_thread(workspace)
            recorder = TraceRecorder()
            handle = recorder.create_run(thread, "screenplay.generate")
            middleware = TraceMiddleware(recorder, handle.trace_id, "interview-subagent")

            request = MagicMock()
            request.tool_call = {"id": "call-1", "name": "ask_user"}

            async def runner() -> None:
                async def _handler(_req):
                    raise GraphInterrupt({"question": "选哪个基调?"})

                with self.assertRaises(GraphInterrupt):
                    await middleware.awrap_tool_call(request, _handler)

            asyncio.run(runner())

            errors = _error_events(recorder, thread, handle.trace_id)
            self.assertEqual(
                errors,
                [],
                "GraphInterrupt 是 HITL 正常控制流，不应记录 tool_error",
            )

    def test_tool_call_real_error_still_recorded(self) -> None:
        """对照组：普通异常仍应记录 tool_error，证明放行逻辑只针对 GraphInterrupt。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            thread = _make_thread(workspace)
            recorder = TraceRecorder()
            handle = recorder.create_run(thread, "screenplay.generate")
            middleware = TraceMiddleware(recorder, handle.trace_id, "writing-subagent")

            request = MagicMock()
            request.tool_call = {"id": "call-2", "name": "write_file"}

            async def runner() -> None:
                async def _handler(_req):
                    raise ValueError("disk full")

                with self.assertRaises(ValueError):
                    await middleware.awrap_tool_call(request, _handler)

            asyncio.run(runner())

            errors = _error_events(recorder, thread, handle.trace_id)
            self.assertEqual(errors, ["tool_error"])


if __name__ == "__main__":
    unittest.main()
