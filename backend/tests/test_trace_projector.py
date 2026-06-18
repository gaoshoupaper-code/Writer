import json
import re
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from app.platform.agent.middleware.trace_middleware import _usage_payload
from app.platform.trace.projector import TraceProjector
from app.platform.trace.recorder import TraceRecorder
from app.platform.trace.schemas import TraceLogEvent, TraceRunSummary
from app.schemas.screenplay import ThreadSummary


class TraceProjectorTest(unittest.TestCase):
    def test_skips_llm_and_tool_inputs_but_keeps_outputs(self) -> None:
        run = TraceRunSummary(
            trace_id="trace-test",
            workspace_id="workspace-test",
            thread_id="thread-test",
            session_name="session",
            workspace_path="/tmp/workspace",
            endpoint="screenplay.generate",
            status="completed",
            started_at="2026-05-22T00:00:00+00:00",
            event_count=4,
            path="traces/thread-test/trace-test.jsonl",
        )
        events = [
            TraceLogEvent(
                trace_id="trace-test",
                event_id="event-1",
                sequence=1,
                type="llm_start",
                status="running",
                timestamp="2026-05-22T00:00:01+00:00",
                source="middleware",
                agent_name="meta-agent",
                model_name="test-model",
                input={
                    "system": "system prompt should not appear",
                    "messages": [
                        {"type": "human", "content": "user prompt should not appear"},
                        {"type": "tool", "content": "tool input history should not appear"},
                    ],
                },
            ),
            TraceLogEvent(
                trace_id="trace-test",
                event_id="event-2",
                sequence=2,
                type="llm_end",
                status="completed",
                timestamp="2026-05-22T00:00:02+00:00",
                source="middleware",
                agent_name="meta-agent",
                model_name="test-model",
                output={"messages": [{"type": "ai", "content": "visible model output"}]},
            ),
            TraceLogEvent(
                trace_id="trace-test",
                event_id="event-3",
                sequence=3,
                type="tool_start",
                status="running",
                timestamp="2026-05-22T00:00:03+00:00",
                source="middleware",
                agent_name="meta-agent",
                tool_name="write_file",
                tool_args={"path": "/outline.md", "content": "tool args should not appear"},
            ),
            TraceLogEvent(
                trace_id="trace-test",
                event_id="event-4",
                sequence=4,
                type="tool_end",
                status="completed",
                timestamp="2026-05-22T00:00:04+00:00",
                source="middleware",
                agent_name="meta-agent",
                tool_name="write_file",
                tool_output={"content": "visible tool output"},
            ),
        ]

        projection = TraceProjector().project(run, events)
        rendered = "\n".join(str(segment.content) for segment in projection.context)
        phases = [segment.metadata.get("phase") for segment in projection.context]

        self.assertEqual(phases, ["output", "output"])
        self.assertIn("visible model output", rendered)
        self.assertIn("visible tool output", rendered)
        self.assertNotIn("system prompt should not appear", rendered)
        self.assertNotIn("user prompt should not appear", rendered)
        self.assertNotIn("tool args should not appear", rendered)


class TraceMiddlewareUsageTest(unittest.TestCase):
    def test_extracts_usage_from_response_metadata_usage(self) -> None:
        message = AIMessage(
            content="调用工具中...",
            response_metadata={"usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}},
            tool_calls=[{"name": "read_file", "args": {"path": "/outline.md"}, "id": "call-1"}],
        )

        usage = _usage_payload([message])

        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})


class TraceRecorderTest(unittest.TestCase):
    def test_create_run_saves_trace_under_minute_timestamp_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            thread = ThreadSummary(
                thread_id="thread-test",
                workspace_id="workspace-test",
                session_name="session",
                workspace_path=str(workspace),
                created_at="2026-05-22T00:00:00+00:00",
                updated_at="2026-05-22T00:00:00+00:00",
            )

            recorder = TraceRecorder()
            handle = recorder.create_run(thread, "screenplay.generate")
            detail = recorder.read_run(thread, handle.trace_id)

            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertRegex(detail.run.path, r"^traces/\d{8}-\d{4}/trace-[0-9a-f]{32}\.jsonl$")
            self.assertTrue((workspace / detail.run.path).exists())

    def test_read_run_sanitizes_legacy_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            trace_dir = workspace / "traces" / "thread-test"
            trace_dir.mkdir(parents=True)
            trace_path = trace_dir / "trace-test.jsonl"
            index_path = workspace / "traces" / "index.json"
            thread = ThreadSummary(
                thread_id="thread-test",
                workspace_id="workspace-test",
                session_name="session",
                workspace_path=str(workspace),
                created_at="2026-05-22T00:00:00+00:00",
                updated_at="2026-05-22T00:00:00+00:00",
            )
            index_path.write_text(
                json.dumps(
                    {
                        "trace-test": {
                            "trace_id": "trace-test",
                            "workspace_id": "workspace-test",
                            "thread_id": "thread-test",
                            "session_name": "session",
                            "workspace_path": str(workspace),
                            "endpoint": "screenplay.generate",
                            "status": "completed",
                            "started_at": "2026-05-22T00:00:00+00:00",
                            "ended_at": "2026-05-22T00:00:03+00:00",
                            "duration_ms": 3,
                            "event_count": 2,
                            "path": "traces/thread-test/trace-test.jsonl",
                        }
                    }
                ),
                encoding="utf-8",
            )
            trace_path.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in [
                        {
                            "trace_id": "trace-test",
                            "event_id": "event-1",
                            "sequence": 1,
                            "type": "llm_start",
                            "status": "running",
                            "timestamp": "2026-05-22T00:00:01+00:00",
                            "source": "middleware",
                            "input": {"system": "hidden"},
                        },
                        {
                            "trace_id": "trace-test",
                            "event_id": "event-2",
                            "sequence": 2,
                            "type": "tool_end",
                            "status": "completed",
                            "timestamp": "2026-05-22T00:00:02+00:00",
                            "source": "middleware",
                            "tool_name": "task",
                            "tool_args": {"prompt": "hidden"},
                            "tool_calls": [{"name": "task", "args": {"prompt": "hidden"}, "id": "call-1"}],
                            "output": {
                                "messages": [
                                    {
                                        "type": "ai",
                                        "content": "visible model output",
                                        "tool_calls": [{"name": "task", "args": {"prompt": "hidden"}, "id": "call-1"}],
                                    }
                                ]
                            },
                            "tool_output": {"content": "visible"},
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            detail = TraceRecorder().read_run(thread, "trace-test")

            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertIsNone(detail.events[0].input)
            self.assertIsNone(detail.events[1].tool_args)
            self.assertEqual(detail.events[1].tool_calls, [{"name": "task", "id": "call-1"}])
            self.assertNotIn("prompt", json.dumps(detail.events[1].output, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
