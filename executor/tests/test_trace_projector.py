import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from app.platform.agent.middleware.trace_middleware import TraceMiddleware, _usage_payload
from app.platform.trace.projector import TraceProjector
from app.platform.trace.recorder import TraceRecorder
from app.platform.trace.schemas import TraceLogEvent, TraceRunSummary
from app.schemas.screenplay import ThreadSummary
from contracts.trace import MiddlewareDescriptor, SkillCatalogEntry


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

    def test_v2_uses_explicit_mechanism_events_instead_of_skill_inference(self) -> None:
        run = TraceRunSummary(
            trace_id="trace-mechanisms",
            workspace_id="workspace",
            thread_id="thread",
            session_name="session",
            workspace_path="",
            endpoint="generate",
            status="completed",
            started_at="2026-01-01T00:00:00+00:00",
            event_count=5,
            path="",
            schema_version=2,
            service="executor",
            workload="creation",
            integrity_status="verified",
        )
        skill = SkillCatalogEntry(
            skill_id="skill-1",
            name="chapter-writing",
            version="2",
            content_hash="a" * 64,
            source="skills/chapter-writing",
            scope="writing-subagent",
            runtime_path="/_skills_0/SKILL.md",
        )
        base = {
            "trace_id": run.trace_id,
            "source": "runtime",
            "agent_name": "writing-subagent",
            "schema_version": 2,
        }
        events = [
            TraceLogEvent(
                **base,
                event_id="event-1",
                sequence=1,
                type="tool_end",
                status="completed",
                timestamp="2026-01-01T00:00:01+00:00",
                tool_name="read_file",
                tool_output={"path": "/_skills_0/SKILL.md", "content": "legacy guess"},
            ),
            TraceLogEvent(
                **base,
                event_id="event-2",
                sequence=2,
                type="skill_activation",
                status="completed",
                timestamp="2026-01-01T00:00:02+00:00",
                skill_name=skill.name,
                skill_catalog=[skill],
                skill_activation={"trigger_event_id": "event-1"},
            ),
            TraceLogEvent(
                **base,
                event_id="event-3",
                sequence=3,
                type="middleware_assembly",
                status="completed",
                timestamp="2026-01-01T00:00:03+00:00",
                middleware_stack=[
                    MiddlewareDescriptor(name="ReadCacheMiddleware", position=0, config_hash="b" * 64)
                ],
            ),
            TraceLogEvent(
                **base,
                event_id="event-4",
                sequence=4,
                type="middleware_intervention",
                status="completed",
                timestamp="2026-01-01T00:00:04+00:00",
                intervention={"action": "cache_hit", "hook": "wrap_tool_call"},
            ),
            TraceLogEvent(
                **base,
                event_id="event-5",
                sequence=5,
                type="hitl",
                timestamp="2026-01-01T00:00:05+00:00",
                status="awaiting_input",
                hitl={"phase": "tool", "state": "requested"},
            ),
        ]

        projection = TraceProjector().project(run, events)
        skill_nodes = [node for node in projection.nodes if node.kind == "skill"]
        middleware_nodes = [node for node in projection.nodes if node.kind == "middleware"]
        hitl_nodes = [node for node in projection.nodes if node.kind == "hitl"]

        self.assertEqual([node.label for node in skill_nodes], ["chapter-writing"])
        self.assertEqual(len(middleware_nodes), 2)
        self.assertEqual(len(hitl_nodes), 1)
        inferred_tool = next(
            node
            for node in projection.nodes
            if node.raw_event_ids == ["event-1"] and node.tool_name == "read_file"
        )
        self.assertEqual(inferred_tool.kind, "tool")


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
            handle = recorder.create_run(
                thread,
                "screenplay.generate",
                traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            )
            detail = recorder.read_run(thread, handle.trace_id)

            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertRegex(detail.run.path, r"^traces/\d{8}-\d{4}/trace-[0-9a-f]{32}\.jsonl$")
            self.assertTrue((workspace / detail.run.path).exists())
            self.assertEqual(detail.run.external_refs["w3c_trace_id"], "4bf92f3577b34da6a3ce929d0e0e4736")
            self.assertEqual(detail.run.external_refs["w3c_parent_span_id"], "00f067aa0ba902b7")

    def test_structural_metadata_redacts_secrets_before_trace_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            thread = ThreadSummary(
                thread_id="thread-secret",
                workspace_id="workspace-secret",
                session_name="Authorization: Bearer sk-1234567890abcdefghijklmnop",
                workspace_path=str(workspace),
                created_at="2026-05-22T00:00:00+00:00",
                updated_at="2026-05-22T00:00:00+00:00",
            )
            recorder = TraceRecorder()
            handle = recorder.create_run(thread, "screenplay.generate")
            recorder._fail_run(
                thread,
                handle.trace_id,
                "upstream failed: access_token=secret-value",
            )

            detail = recorder.read_run(thread, handle.trace_id)
            assert detail is not None
            self.assertEqual(detail.run.session_name, "Authorization=[redacted]")
            self.assertEqual(detail.run.error, "upstream failed: access_token=[redacted]")
            self.assertNotIn("secret-value", detail.events[-1].error or "")

    def test_terminal_flush_failure_is_fail_open_and_marks_run_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            thread = ThreadSummary(
                thread_id="thread-write-failure",
                workspace_id="workspace-write-failure",
                session_name="session",
                workspace_path=str(workspace),
                created_at="2026-05-22T00:00:00+00:00",
                updated_at="2026-05-22T00:00:00+00:00",
            )
            recorder = TraceRecorder()
            handle = recorder.create_run(thread, "screenplay.generate")
            recorder._drain_task = SimpleNamespace(done=lambda: False)
            original_open = Path.open

            def fail_append(path: Path, mode: str = "r", *args, **kwargs):
                if "a" in mode:
                    raise OSError("simulated trace disk failure")
                return original_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", fail_append):
                recorder.complete_run(thread, handle.trace_id)

            run = recorder.find_run_by_trace_id(handle.trace_id)
            self.assertIsNotNone(run)
            assert run is not None
            self.assertEqual(run.status, "completed")
            self.assertEqual(run.integrity_status, "incomplete")
            self.assertIsNone(run.manifest)

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

    def test_v2_model_payload_is_full_and_manifest_covers_final_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            thread = ThreadSummary(
                thread_id="thread-v2",
                workspace_id="workspace-v2",
                session_name="session",
                workspace_path=str(workspace),
                created_at="2026-05-22T00:00:00+00:00",
                updated_at="2026-05-22T00:00:00+00:00",
            )
            recorder = TraceRecorder()
            handle = recorder.create_run(thread, "screenplay.generate")
            long_prompt = "开" + "正文" * 20_000 + "终"
            request = SimpleNamespace(
                messages=[HumanMessage(content=long_prompt)],
                system_message=None,
                model=SimpleNamespace(model_name="test-model"),
            )

            TraceMiddleware(recorder, handle.trace_id, "writer")._record_model_start(request)
            recorder.set_prompt_version(handle.trace_id, "writer", 3)
            terminal = recorder.complete_run(thread, handle.trace_id)

            run = recorder.find_run_by_trace_id(handle.trace_id)
            self.assertIsNotNone(run)
            assert run is not None and run.manifest is not None
            self.assertEqual(run.manifest.final_sequence, run.event_count)
            self.assertEqual(run.manifest.terminal_event_id, terminal.event_id)
            events = recorder.read_trace_events(handle.trace_id) or []
            llm_start = next(event for event in events if event.type == "llm_start")
            payload = recorder.read_payload(
                handle.trace_id, llm_start.payload_refs["input"].payload_id
            )
            self.assertEqual(payload["messages"][0]["content"], long_prompt)

    def test_artifact_revision_requires_payload_and_chains_across_traces(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            thread = ThreadSummary(
                thread_id="thread-artifact",
                workspace_id="workspace-artifact",
                session_name="session",
                workspace_path=str(workspace),
                created_at="2026-05-22T00:00:00+00:00",
                updated_at="2026-05-22T00:00:00+00:00",
            )
            recorder = TraceRecorder()
            first_run = recorder.create_run(thread, "/screenplay/generate")
            first_revision = recorder.record_artifact_revision(
                first_run.trace_id,
                "writing-subagent",
                file_path="/chapter/1.md",
                content="version one",
                tool_name="write_file",
            )
            second_run = recorder.create_run(thread, "/screenplay/generate")
            second_revision = recorder.record_artifact_revision(
                second_run.trace_id,
                "writing-subagent",
                file_path="/chapter/1.md",
                content="version two",
                tool_name="edit_file",
            )

            second_event = next(
                event
                for event in recorder.read_trace_events(second_run.trace_id) or []
                if event.artifact_revision_id == second_revision
            )
            self.assertEqual(second_event.artifact["parent_revision_id"], first_revision)
            self.assertIn("output", second_event.payload_refs)
            self.assertIsNone(second_event.output)
            self.assertEqual(
                recorder.read_payload(second_run.trace_id, second_event.payload_refs["output"].payload_id),
                {"content": "version two"},
            )

    def test_rejected_artifact_payload_does_not_create_revision_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            thread = ThreadSummary(
                thread_id="thread-artifact-reject",
                workspace_id="workspace-artifact-reject",
                session_name="session",
                workspace_path=str(workspace),
                created_at="2026-05-22T00:00:00+00:00",
                updated_at="2026-05-22T00:00:00+00:00",
            )
            recorder = TraceRecorder()
            run = recorder.create_run(thread, "/screenplay/generate")

            with self.assertRaises(ValueError):
                recorder.record_artifact_revision(
                    run.trace_id,
                    "writing-subagent",
                    file_path="/chapter/1.md",
                    content="sk-1234567890abcdefghijklmnop",
                )

            self.assertFalse(
                any(event.type == "artifact_revision" for event in recorder.read_trace_events(run.trace_id) or [])
            )

    def test_artifact_revision_capture_is_idempotent_and_detects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            thread = ThreadSummary(
                thread_id="thread-artifact-idempotent",
                workspace_id="workspace-artifact-idempotent",
                session_name="session",
                workspace_path=str(workspace),
                created_at="2026-05-22T00:00:00+00:00",
                updated_at="2026-05-22T00:00:00+00:00",
            )
            recorder = TraceRecorder()
            run = recorder.create_run(thread, "/screenplay/generate")

            first = recorder.record_artifact_revision(
                run.trace_id,
                "writing-subagent",
                file_path="/chapter/1.md",
                content="same content",
                tool_name="write_file",
                tool_call_id="call-1",
            )
            replay = recorder.record_artifact_revision(
                run.trace_id,
                "writing-subagent",
                file_path="/chapter/1.md",
                content="same content",
                tool_name="write_file",
                tool_call_id="call-1",
            )
            second_call = recorder.record_artifact_revision(
                run.trace_id,
                "writing-subagent",
                file_path="/chapter/1.md",
                content="same content",
                tool_name="write_file",
                tool_call_id="call-2",
            )

            self.assertEqual(replay, first)
            self.assertNotEqual(second_call, first)
            revisions = [
                event
                for event in recorder.read_trace_events(run.trace_id) or []
                if event.type == "artifact_revision"
            ]
            self.assertEqual(len(revisions), 2)

            with self.assertRaises(ValueError):
                recorder.record_artifact_revision(
                    run.trace_id,
                    "writing-subagent",
                    file_path="/chapter/1.md",
                    content="conflicting content",
                    tool_name="edit_file",
                    tool_call_id="call-1",
                )

    def test_strict_capture_failure_has_distinct_trace_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            thread = ThreadSummary(
                thread_id="thread-capture-failure",
                workspace_id="workspace-capture-failure",
                session_name="session",
                workspace_path=tmpdir,
                created_at="2026-05-22T00:00:00+00:00",
                updated_at="2026-05-22T00:00:00+00:00",
            )
            recorder = TraceRecorder()
            run = recorder.create_run(thread, "/screenplay/ab")

            recorder.fail_evidence_capture_run(thread, run.trace_id, OSError("payload down"))

            summary = recorder.find_run_by_trace_id(run.trace_id)
            self.assertIsNotNone(summary)
            self.assertEqual(summary.status, "evidence_capture_failed")
            terminal = (recorder.read_trace_events(run.trace_id) or [])[-1]
            self.assertEqual(terminal.status, "evidence_capture_failed")

    def test_skill_activation_is_bound_to_registered_runtime_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            skill_dir = workspace / "skill-source" / "chapter-writing"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "---\nname: chapter-writing\nversion: 7\ndescription: test\n---\nbody",
                encoding="utf-8",
            )
            thread = ThreadSummary(
                thread_id="thread-skill",
                workspace_id="workspace-skill",
                session_name="session",
                workspace_path=str(workspace),
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
            recorder = TraceRecorder()
            handle = recorder.create_run(thread, "screenplay.generate")
            recorder.record_skill_catalog(
                handle.trace_id,
                "writing-subagent",
                [str(skill_dir)],
                ["/_skills_0"],
            )
            middleware = TraceMiddleware(recorder, handle.trace_id, "writing-subagent")
            registered = SimpleNamespace(
                tool_call={
                    "id": "call-1",
                    "name": "read_file",
                    "args": {"file_path": "/_skills_0/SKILL.md"},
                }
            )
            ordinary = SimpleNamespace(
                tool_call={
                    "id": "call-2",
                    "name": "read_file",
                    "args": {"file_path": "/outline.md"},
                }
            )

            middleware._record_tool_end(registered, "body", 0.0)
            middleware._record_tool_end(ordinary, "body", 0.0)
            middleware._record_tool_error(registered, 0.0, FileNotFoundError("missing"))

            activations = [
                event
                for event in (recorder.read_trace_events(handle.trace_id) or [])
                if event.type == "skill_activation"
            ]
            self.assertEqual([event.status for event in activations], ["completed", "failed"])
            self.assertEqual(activations[0].skill_catalog[0].version, "7")
            self.assertEqual(
                activations[0].skill_activation["trigger_event_id"],
                activations[0].parent_event_id,
            )


if __name__ == "__main__":
    unittest.main()
