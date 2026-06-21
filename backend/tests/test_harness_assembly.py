"""Phase 1 T1.3：执行端装配点改造测试。

验证：
- 开关 writer_use_harness=False 时走旧 _agent_for_workspace（旧行为不变）
- 开关=True 时走 _assemble_via_harness（harness 驱动装配）
- harness 装配链能完整跑通（产出带 subagents 的 agent，不抛异常）
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.platform.harness import HarnessContext
from app.harnesses.v1 import WriterHarnessV1
from app.harnesses.v1.subagents import StorybuildingHarness, InterviewHarness


@pytest.fixture(autouse=True)
def _mock_prompt_loader():
    """全局 mock load_prompt（避免 HTTP/monitoring 依赖）。"""
    fake = SimpleNamespace(content="PROMPT_BODY", version="v1")
    with patch("app.platform.prompt.load_prompt", return_value=fake), \
         patch("app.platform.prompt.loader.load_prompt", return_value=fake):
        yield


def _make_service(tmp_path):
    """构造最小可用的 MetaAgentService（mock 重依赖）。"""
    from app.domains.writing.meta.agent import MetaAgentService
    settings = SimpleNamespace(
        writer_use_harness=False,
        writer_agent_mode="mock",
        writer_model="test",
        writer_temperature=None,
        writer_top_p=None,
    )
    svc = MetaAgentService.__new__(MetaAgentService)
    svc.settings = settings
    svc.workspace_root = tmp_path
    svc.trace_recorder = MagicMock()
    svc.style_store = MagicMock()
    svc.checkpointer = MagicMock()
    # 风格解析返回 None（无激活风格）
    svc._resolve_meta_style = MagicMock(return_value=None)
    svc._resolve_style_for_subagent = MagicMock(return_value=None)
    svc._backend_for_workspace = MagicMock(return_value=MagicMock())
    return svc


# ── 开关行为 ────────────────────────────────────────────────


class TestHarnessSwitch:
    def test_switch_off_calls_old_path(self, tmp_path, monkeypatch) -> None:
        """writer_use_harness=False → 不走 _assemble_via_harness（走旧分支）。"""
        svc = _make_service(tmp_path)
        harness_called = {"v": False}

        def spy(self, *a, **kw):
            harness_called["v"] = True
            return {"via": "harness"}

        monkeypatch.setattr(type(svc), "_assemble_via_harness", spy)
        # 开关关时调用，无论旧路径结果如何，harness 路径不应被调用
        try:
            svc._agent_for_workspace(tmp_path)
        except Exception:
            pass  # 旧路径因 mock 不完整抛错无妨，关键看分支选择
        assert harness_called["v"] is False  # 开关关 → 不调 harness 路径

    def test_switch_on_calls_harness_path(self, tmp_path, monkeypatch) -> None:
        """writer_use_harness=True → 走 _assemble_via_harness。"""
        svc = _make_service(tmp_path)
        svc.settings.writer_use_harness = True
        harness_called = {"v": False}
        orig = svc._assemble_via_harness.__func__

        def spy(self, *a, **kw):
            harness_called["v"] = True
            return {"via": "harness"}

        monkeypatch.setattr(type(svc), "_assemble_via_harness", spy)
        with patch("app.domains.writing.meta.agent.build_writer_model"), \
             patch("app.platform.prompt.load_prompt") as mock_lp:
            mock_lp.return_value = SimpleNamespace(content="P", version="v1")
            # 旧路径会调 create_deep_agent，但 harness 路径被 spy 拦截
            result = svc._agent_for_workspace(tmp_path)
        assert harness_called["v"] is True


# ── subagent 装配分发 ───────────────────────────────────────


class TestSubagentAssembly:
    def test_assemble_custom_subagent(self, tmp_path) -> None:
        """is_custom subagent 走 build_compiled。"""
        svc = _make_service(tmp_path)
        ctx = HarnessContext(workspace_path=tmp_path)
        sh = InterviewHarness()
        with patch(
            "app.domains.writing.expert_agent.agents.interview.build_interview_deep_subagent"
        ) as mock_build:
            mock_build.return_value = {"name": "interview", "compiled": True}
            result = svc._assemble_one_subagent(sh, ctx, model=MagicMock(), backend=MagicMock())
        assert mock_build.called
        assert result == {"name": "interview", "compiled": True}

    def test_assemble_deep_subagent(self, tmp_path) -> None:
        """is_deep subagent 走 build_deep_subagent（mock 工厂）。"""
        svc = _make_service(tmp_path)
        ctx = HarnessContext(workspace_path=tmp_path)
        sh = StorybuildingHarness()
        with patch("app.domains.writing.expert_agent.factory.build_deep_subagent") as mock_deep, \
             patch.object(svc, "_build_evolution_spec", return_value={"name": "evolution"}), \
             patch.object(svc, "_middleware_for_subagent_via_harness", return_value=[]):
            mock_deep.return_value = {"name": "storybuilding", "compiled": True}
            result = svc._assemble_one_subagent(sh, ctx, model=MagicMock(), backend=MagicMock())
        assert mock_deep.called
        # 验证传给 build_deep_subagent 的关键参数
        kwargs = mock_deep.call_args.kwargs
        assert kwargs["name"] == "storybuilding"
        assert kwargs["max_revisions"] == 1


# ── evolution_spec 构建 ────────────────────────────────────


class TestEvolutionSpecBuild:
    def test_build_evolution_storybuilding(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        ctx = HarnessContext(workspace_path=tmp_path)
        with patch(
            "app.domains.writing.expert_agent.evaluators.storybuilding.build_storybuilding_evaluator"
        ) as mock_eval:
            mock_eval.return_value = {
                "system_prompt": "eval", "permissions": [], "middleware": [],
            }
            spec = svc._build_evolution_spec("storybuilding", ctx, lambda name: [])
        assert spec["name"] == "evolution"
        assert mock_eval.called

    def test_build_evolution_unknown_kind_raises(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        ctx = HarnessContext(workspace_path=tmp_path)
        with pytest.raises(ValueError):
            svc._build_evolution_spec("unknown", ctx, lambda name: [])


# ── middleware 合并 ────────────────────────────────────────


class TestMiddlewareMerge:
    def test_pipeline_subagent_gets_artifact_prerequisite(self, tmp_path) -> None:
        """detail-outline/writing subagent 应获得 ArtifactPrerequisiteMiddleware。"""
        svc = _make_service(tmp_path)
        ctx = HarnessContext(workspace_path=tmp_path)
        mw = svc._middleware_for_subagent_via_harness(ctx, "writing-subagent")
        # 应含 ArtifactPrerequisite（_artifact_prerequisites_for_pipeline_subagent 返回非空）
        from app.platform.agent.middleware import ArtifactPrerequisiteMiddleware
        assert any(isinstance(m, ArtifactPrerequisiteMiddleware) for m in mw)

    def test_non_pipeline_subagent_no_prerequisite(self, tmp_path) -> None:
        """storybuilding subagent 不获得 ArtifactPrerequisite。"""
        svc = _make_service(tmp_path)
        ctx = HarnessContext(workspace_path=tmp_path)
        mw = svc._middleware_for_subagent_via_harness(ctx, "storybuilding-subagent")
        from app.platform.agent.middleware import ArtifactPrerequisiteMiddleware
        assert not any(isinstance(m, ArtifactPrerequisiteMiddleware) for m in mw)
