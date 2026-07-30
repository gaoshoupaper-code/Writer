from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import ToolMessage

from app.platform.agent.middleware.artifact_capture import (
    EvidenceCaptureError,
    PlatformArtifactCaptureMiddleware,
)
from app.platform.agent.runtime import factory


class _Recorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.captures: list[dict] = []
        self.degraded: list[str] = []

    def record_artifact_revision(self, trace_id: str, agent_name: str, **values):
        if self.fail:
            raise OSError("payload store unavailable")
        self.captures.append({"trace_id": trace_id, "agent_name": agent_name, **values})
        return "revision-1"

    def _mark_capture_degraded(self, trace_id: str, reason: str) -> None:
        self.degraded.append(f"{trace_id}:{reason}")


class PlatformArtifactCaptureTest(unittest.TestCase):
    def test_successful_write_is_read_back_after_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            recorder = _Recorder()
            middleware = PlatformArtifactCaptureMiddleware(
                recorder=recorder,
                trace_id="trace-1",
                workspace_root=workspace,
                agent_name="writing-subagent",
                strict=False,
            )
            request = SimpleNamespace(
                tool_call={
                    "id": "call-1",
                    "name": "write_file",
                    "args": {"file_path": "/chapter/chapter-01.md"},
                }
            )

            def handler(_request):
                target = workspace / "chapter" / "chapter-01.md"
                target.parent.mkdir(parents=True)
                target.write_text("正文", encoding="utf-8")
                return ToolMessage(content="ok", tool_call_id="call-1")

            result = middleware.wrap_tool_call(request, handler)

            self.assertEqual(result.content, "ok")
            self.assertEqual(recorder.captures[0]["content"], "正文")
            self.assertEqual(recorder.captures[0]["tool_call_id"], "call-1")

    def test_normal_creation_fails_open_but_strict_test_terminates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "review" / "final.md"
            target.parent.mkdir(parents=True)
            target.write_text("review", encoding="utf-8")
            request = SimpleNamespace(
                tool_call={
                    "id": "call-2",
                    "name": "edit_file",
                    "args": {"file_path": "/review/final.md"},
                }
            )
            result = ToolMessage(content="ok", tool_call_id="call-2")

            fail_open_recorder = _Recorder(fail=True)
            fail_open = PlatformArtifactCaptureMiddleware(
                recorder=fail_open_recorder,
                trace_id="trace-user",
                workspace_root=workspace,
                agent_name="review-subagent",
                strict=False,
            )
            self.assertIs(fail_open.wrap_tool_call(request, lambda _: result), result)
            self.assertTrue(fail_open_recorder.degraded)

            strict_recorder = _Recorder(fail=True)
            strict = PlatformArtifactCaptureMiddleware(
                recorder=strict_recorder,
                trace_id="trace-test",
                workspace_root=workspace,
                agent_name="review-subagent",
                strict=True,
            )
            with self.assertRaises(EvidenceCaptureError):
                strict.wrap_tool_call(request, lambda _: result)

    def test_runtime_factory_prepends_capture_to_agent_and_specs(self) -> None:
        recorder = _Recorder()
        marker = object()
        subagent = {
            "name": "general-purpose",
            "description": "test",
            "system_prompt": "test",
            "middleware": [marker],
        }
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            factory, "_create_deep_agent", side_effect=lambda *args, **kwargs: kwargs
        ):
            with factory.artifact_capture_scope(
                recorder=recorder,
                trace_id="trace-scope",
                workspace_root=Path(tmpdir),
                strict=True,
            ):
                built = factory.create_deep_agent(
                    middleware=[marker],
                    subagents=[subagent],
                )

        self.assertIsInstance(built["middleware"][0], PlatformArtifactCaptureMiddleware)
        self.assertIs(built["middleware"][1], marker)
        self.assertIsInstance(
            built["subagents"][0]["middleware"][0], PlatformArtifactCaptureMiddleware
        )
        self.assertIs(built["subagents"][0]["middleware"][1], marker)


if __name__ == "__main__":
    unittest.main()
