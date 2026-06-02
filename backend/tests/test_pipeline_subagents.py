from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda

from app.core.settings import Settings
from app.writer.middleware import GoalMiddleware
from app.writer.meta_agent import MetaAgentService
from app.writer.subagents.outline.outline_subagent import _build_compiled_pipeline_subagent
from app.writer.trace import TraceRecorder


class _FakeStyleStore:
    """Minimal style store stub for tests."""
    def get_active_style_id(self, workspace_id: str) -> str | None:
        return None
    def get_style(self, style_id: str) -> dict | None:
        return None


class PipelineSubagentsTest(unittest.TestCase):
    def test_outline_pipeline_runs_evaluation_after_outline_success(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            def outline_agent(state: dict[str, Any]) -> dict[str, list[AIMessage]]:
                calls.append("outline")
                (workspace / "outline.md").write_text("outline", encoding="utf-8")
                return {"messages": [AIMessage(content="outline done")]}

            def evaluation_agent(state: dict[str, Any]) -> dict[str, list[AIMessage]]:
                calls.append("evaluation")
                (workspace / "evaluation.md").write_text("evaluation", encoding="utf-8")
                return {"messages": [AIMessage(content="evaluation done")]}

            spec = _build_compiled_pipeline_subagent(
                name="outline",
                description="outline pipeline",
                workspace_root=workspace,
                primary_agent=RunnableLambda(outline_agent),
                secondary_agent=RunnableLambda(evaluation_agent),
                primary_artifact="outline.md",
                secondary_artifact="evaluation.md",
                primary_label="outline",
                secondary_label="evaluation",
                secondary_instruction="evaluate outline",
            )

            result = spec["runnable"].invoke({"messages": [HumanMessage(content="build outline")]})

        self.assertEqual(calls, ["outline", "evaluation"])
        self.assertIn("outline.md：已写入或更新", result["messages"][-1].content)
        self.assertIn("evaluation.md：已写入", result["messages"][-1].content)

    def test_outline_pipeline_does_not_run_evaluation_when_outline_missing(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            def outline_agent(state: dict[str, Any]) -> dict[str, list[AIMessage]]:
                calls.append("outline")
                return {"messages": [AIMessage(content="outline skipped")]}

            def evaluation_agent(state: dict[str, Any]) -> dict[str, list[AIMessage]]:
                calls.append("evaluation")
                (workspace / "evaluation.md").write_text("evaluation", encoding="utf-8")
                return {"messages": [AIMessage(content="evaluation done")]}

            spec = _build_compiled_pipeline_subagent(
                name="outline",
                description="outline pipeline",
                workspace_root=workspace,
                primary_agent=RunnableLambda(outline_agent),
                secondary_agent=RunnableLambda(evaluation_agent),
                primary_artifact="outline.md",
                secondary_artifact="evaluation.md",
                primary_label="outline",
                secondary_label="evaluation",
                secondary_instruction="evaluate outline",
            )

            with self.assertRaises(FileNotFoundError):
                spec["runnable"].invoke({"messages": [HumanMessage(content="build outline")]})

        self.assertEqual(calls, ["outline"])

    def test_writing_pipeline_runs_review_after_writing_success(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            def writing_agent(state: dict[str, Any]) -> dict[str, list[AIMessage]]:
                calls.append("writing")
                (workspace / "novel.md").write_text("novel", encoding="utf-8")
                return {"messages": [AIMessage(content="writing done")]}

            def review_agent(state: dict[str, Any]) -> dict[str, list[AIMessage]]:
                calls.append("review")
                (workspace / "review").mkdir()
                (workspace / "review" / "chapter-01.md").write_text("review", encoding="utf-8")
                return {"messages": [AIMessage(content="review done")]}

            spec = _build_compiled_pipeline_subagent(
                name="writing",
                description="writing pipeline",
                workspace_root=workspace,
                primary_agent=RunnableLambda(writing_agent),
                secondary_agent=RunnableLambda(review_agent),
                primary_artifact="novel.md",
                secondary_artifact="review/",
                primary_label="writing",
                secondary_label="review",
                secondary_instruction="review latest chapter",
            )

            result = spec["runnable"].invoke({"messages": [HumanMessage(content="write scene")]})

        self.assertEqual(calls, ["writing", "review"])
        self.assertIn("novel.md：已写入或更新", result["messages"][-1].content)
        self.assertIn("review/：已写入或更新", result["messages"][-1].content)

    def test_writing_pipeline_does_not_run_review_when_novel_missing(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            def writing_agent(state: dict[str, Any]) -> dict[str, list[AIMessage]]:
                calls.append("writing")
                return {"messages": [AIMessage(content="writing skipped")]}

            def review_agent(state: dict[str, Any]) -> dict[str, list[AIMessage]]:
                calls.append("review")
                (workspace / "review").mkdir()
                (workspace / "review" / "chapter-01.md").write_text("review", encoding="utf-8")
                return {"messages": [AIMessage(content="review done")]}

            spec = _build_compiled_pipeline_subagent(
                name="writing",
                description="writing pipeline",
                workspace_root=workspace,
                primary_agent=RunnableLambda(writing_agent),
                secondary_agent=RunnableLambda(review_agent),
                primary_artifact="novel.md",
                secondary_artifact="review/",
                primary_label="writing",
                secondary_label="review",
                secondary_instruction="review latest chapter",
            )

            with self.assertRaises(FileNotFoundError):
                spec["runnable"].invoke({"messages": [HumanMessage(content="write scene")]})

        self.assertEqual(calls, ["writing"])


class MetaAgentSubagentRegistrationTest(unittest.TestCase):
    def test_outline_and_writing_are_compiled_subagents(self) -> None:
        settings = Settings(
            writer_model="openai:test-model",
            writer_agent_mode="live",
            writer_frontend_origin="http://localhost:5173",
            openai_api_key="test-key",
            openai_base_url="http://localhost:1234/v1",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            service = MetaAgentService(settings, workspace, TraceRecorder(), _FakeStyleStore())

            outline = service._outline_subagent_for_workspace(workspace)
            writing = service._writing_subagent_for_workspace(workspace)

        self.assertEqual(outline["name"], "outline")
        self.assertIn("runnable", outline)
        self.assertEqual(writing["name"], "writing")
        self.assertIn("runnable", writing)

    def test_subagent_specs_receive_goal_middleware(self) -> None:
        settings = Settings(
            writer_model="openai:test-model",
            writer_agent_mode="live",
            writer_frontend_origin="http://localhost:5173",
            openai_api_key="test-key",
            openai_base_url="http://localhost:1234/v1",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            service = MetaAgentService(settings, workspace, TraceRecorder(), _FakeStyleStore())
            subagents = [
                service._general_subagent_for_workspace(workspace),
                service._character_subagent_for_workspace(workspace),
            ]

        for subagent in subagents:
            with self.subTest(name=subagent["name"]):
                middleware = subagent.get("middleware", [])
                self.assertTrue(any(isinstance(item, GoalMiddleware) for item in middleware))


if __name__ == "__main__":
    unittest.main()
