"""Phase 1 T1.2：harness v1 等价性测试。

验证 harness v1 的 build 方法产出与现有 builder 逻辑等价（同源 prompt/相同 skills/
相同 permissions）。这是 T1.4 完整等价性的参数级前置验证。

等价性策略：harness v1 build 方法内部复用现有 builder 的装配逻辑（load_prompt/
FilesystemPermission/同 skill 路径），所以参数同源 → 装配结果等价。
"""
from sys import version_info
from pathlib import Path
from unittest.mock import patch

import pytest

from app.platform.harness import HarnessContext
from app.harnesses.v1 import WriterHarnessV1
from app.harnesses.v1.subagents import (
    DetailOutlineHarness,
    InterviewHarness,
    StorybuildingHarness,
    WritingHarness,
)


# mock load_prompt 避免 HTTP 依赖（monitoring 可能未起）
_FAKE_PROMPT = PromptContent = type("PromptContent", (), {"content": "PROMPT_BODY", "version": "v1"})


@pytest.fixture
def ctx(tmp_path) -> HarnessContext:
    return HarnessContext(workspace_path=tmp_path)


@pytest.fixture(autouse=True)
def _mock_prompt():
    with patch("app.platform.prompt.load_prompt", return_value=_FAKE_PROMPT):
        yield


# ── WriterHarnessV1 ────────────────────────────────────────


class TestWriterHarnessV1:
    def test_harness_id(self) -> None:
        assert WriterHarnessV1().harness_id() == "writer-harness-v1"

    def test_build_system_prompt_no_style(self, ctx) -> None:
        prompt = WriterHarnessV1().build_system_prompt(ctx)
        assert prompt == "PROMPT_BODY"

    def test_build_system_prompt_with_style(self, ctx) -> None:
        ctx.meta_style = "宏大叙事"
        prompt = WriterHarnessV1().build_system_prompt(ctx)
        assert "【主控风格】" in prompt
        assert "宏大叙事" in prompt

    def test_build_skills_returns_two_paths(self, ctx) -> None:
        skills = WriterHarnessV1().build_skills(ctx)
        assert len(skills) == 2
        assert any("auto-pipeline" in s for s in skills)
        assert any("interactive-gating" in s for s in skills)

    def test_build_tools_empty(self, ctx) -> None:
        assert WriterHarnessV1().build_tools(ctx) == []

    def test_build_middleware_has_goal_and_readonly(self, ctx) -> None:
        from app.domains.writing.middleware import GoalMiddleware, MetaReadOnlyMiddleware
        mw = WriterHarnessV1().build_middleware(ctx)
        types = [type(m) for m in mw]
        assert GoalMiddleware in types
        assert MetaReadOnlyMiddleware in types

    def test_build_subagents_returns_four(self, ctx) -> None:
        subs = WriterHarnessV1().build_subagents(ctx)
        names = [s.name for s in subs]
        assert set(names) == {"interview", "storybuilding", "detail-outline", "writing"}


# ── StorybuildingHarness ───────────────────────────────────


class TestStorybuildingHarness:
    def test_is_deep(self) -> None:
        assert StorybuildingHarness().is_deep is True

    def test_name(self) -> None:
        assert StorybuildingHarness().name == "storybuilding"

    def test_system_prompt_uses_style(self, ctx) -> None:
        ctx.storybuilding_style = "奇幻"
        h = StorybuildingHarness()
        # apply_style_suffix 会追加 style
        prompt = h.build_system_prompt(ctx)
        assert "PROMPT_BODY" in prompt

    def test_permissions_match_existing(self, ctx) -> None:
        """permissions 与现有 build_storybuilding_subagent 一致（4 allow + 1 deny）。"""
        h = StorybuildingHarness()
        perms = h.build_permissions(ctx)
        # 现有：1 read allow + 4 write allow + 1 write deny
        assert len(perms) == 6
        write_allows = [
            p for p in perms
            if "write" in p.operations and p.mode == "allow"
        ]
        assert len(write_allows) == 4  # character/worldview/storyline.md/storyline/*

    def test_skills_two_paths(self, ctx) -> None:
        skills = StorybuildingHarness().build_skills(ctx)
        assert len(skills) == 2
        assert any("storybuilding-initial" in s for s in skills)
        assert any("storybuilding-expand" in s for s in skills)

    def test_deep_params_structure(self, ctx) -> None:
        params = StorybuildingHarness().build_deep_params(ctx)
        assert params["max_revisions"] == 1
        assert params["evaluator_kind"] == "storybuilding"
        assert len(params["artifact_paths"]) == 3
        assert "system_prompt" in params
        assert "skills" in params


# ── DetailOutlineHarness ───────────────────────────────────


class TestDetailOutlineHarness:
    def test_is_deep(self) -> None:
        assert DetailOutlineHarness().is_deep is True

    def test_permissions_write_detail_only(self, ctx) -> None:
        perms = DetailOutlineHarness().build_permissions(ctx)
        write_allows = [
            p for p in perms
            if "write" in p.operations and p.mode == "allow"
        ]
        assert len(write_allows) == 1
        assert write_allows[0].paths == ["/detail/**"]

    def test_deep_params_evaluator_kind(self, ctx) -> None:
        assert DetailOutlineHarness().build_deep_params(ctx)["evaluator_kind"] == "detail-outline"


# ── WritingHarness ─────────────────────────────────────────


class TestWritingHarness:
    def test_is_deep(self) -> None:
        assert WritingHarness().is_deep is True

    def test_permissions_write_chapter_only(self, ctx) -> None:
        perms = WritingHarness().build_permissions(ctx)
        write_allows = [
            p for p in perms
            if "write" in p.operations and p.mode == "allow"
        ]
        assert len(write_allows) == 1
        assert write_allows[0].paths == ["/chapter/**"]

    def test_deep_params_evaluator_kind(self, ctx) -> None:
        assert WritingHarness().build_deep_params(ctx)["evaluator_kind"] == "writing"


# ── InterviewHarness ───────────────────────────────────────


class TestInterviewHarness:
    def test_is_custom(self) -> None:
        assert InterviewHarness().is_custom is True

    def test_is_not_deep(self) -> None:
        assert InterviewHarness().is_deep is False

    def test_build_compiled_without_assembler_returns_none(self, ctx) -> None:
        assert InterviewHarness().build_compiled(ctx) is None

    def test_build_compiled_calls_existing_builder(self, ctx) -> None:
        """build_compiled 有 assembler 时调用现有 build_interview_deep_subagent。"""
        with patch(
            "app.domains.writing.expert_agent.agents.interview.build_interview_deep_subagent"
        ) as mock_build:
            mock_build.return_value = {"name": "interview", "compiled": True}
            assembler = {
                "model": object(),
                "backend": object(),
                "middleware_factory": lambda name: [],
            }
            result = InterviewHarness().build_compiled(ctx, assembler=assembler)
            assert mock_build.called
            assert result == {"name": "interview", "compiled": True}
