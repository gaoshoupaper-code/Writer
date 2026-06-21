"""Phase 1 T1.4：harness 装配链等价性验证。

这是 Phase 1 命门：证明开关打开后 _assemble_via_harness 能完整跑通装配链，
产出的 agent 与旧路径结构等价（带 5 个 subagents + 正确 middleware 栈）。

等价性证据链：
  - T1.2 已证参数级等价（prompt/skills/permissions 同源）
  - 本测试证装配链可跑通（不抛异常 + 产出带 5 subagents 的 create_deep_agent 结果）
  - 完整行为等价（生成的小说一样）靠后续开关打开后真跑验证

注意：本测试 mock model/backend（避免真 LLM），但走完整装配代码路径。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.platform.harness import HarnessContext


def _make_full_service(tmp_path):
    """构造接近真实的 MetaAgentService（mock 重依赖，但保留装配逻辑）。"""
    from app.domains.writing.meta.agent import MetaAgentService
    svc = MetaAgentService.__new__(MetaAgentService)
    svc.settings = SimpleNamespace(
        writer_use_harness=True,
        writer_agent_mode="live",
        writer_model="openai:gpt-4o-mini",
        writer_temperature=None,
        writer_top_p=None,
    )
    svc.workspace_root = tmp_path
    svc.trace_recorder = MagicMock()
    svc.style_store = MagicMock()
    svc.checkpointer = MagicMock()
    svc._resolve_meta_style = MagicMock(return_value=None)
    svc._resolve_style_for_subagent = MagicMock(return_value=None)
    svc._backend_for_workspace = MagicMock(return_value=MagicMock())
    return svc


class TestHarnessAssemblyEquivalence:
    """harness 装配链完整跑通 + 结构等价。"""

    def test_assemble_via_harness_produces_agent(self, tmp_path) -> None:
        """开关开 → _assemble_via_harness 完整跑通，产出 agent。"""
        svc = _make_full_service(tmp_path)
        fake_prompt = SimpleNamespace(content="PROMPT", version="v1")

        with patch("app.platform.prompt.load_prompt", return_value=fake_prompt), \
             patch("app.platform.prompt.loader.load_prompt", return_value=fake_prompt), \
             patch("app.domains.writing.meta.agent.build_writer_model", return_value=MagicMock()), \
             patch("app.domains.writing.meta.agent.create_deep_agent") as mock_cda, \
             patch("app.domains.writing.meta.agent.compose_skills_backend") as mock_compose, \
             patch("app.domains.writing.expert_agent.factory.build_deep_subagent") as mock_deep, \
             patch.object(svc, "_build_evolution_spec", return_value={"name": "evolution"}), \
             patch(
                 "app.domains.writing.expert_agent.agents.interview.build_interview_deep_subagent",
                 return_value=MagicMock(),
             ):
            mock_compose.return_value = (MagicMock(), [])
            mock_cda.return_value = MagicMock(name="assembled_meta_agent")
            mock_deep.return_value = MagicMock(name="compiled_subagent")

            agent = svc._assemble_via_harness(tmp_path, "trace-1", "ws-1")

        # 核心断言：create_deep_agent 被调用一次（meta agent 装配）
        assert mock_cda.called
        cda_kwargs = mock_cda.call_args.kwargs
        # system_prompt 来自 harness（非空）
        assert cda_kwargs["system_prompt"]
        # subagents 列表非空（general + 4 harness subagents = 5）
        assert len(cda_kwargs["subagents"]) == 5
        # tools 空（meta 无工具）
        assert cda_kwargs["tools"] == []

    def test_assemble_calls_deep_factory_three_times(self, tmp_path) -> None:
        """storybuilding/detail-outline/writing 三个 deep subagent 各调一次 build_deep_subagent。"""
        svc = _make_full_service(tmp_path)
        fake_prompt = SimpleNamespace(content="PROMPT", version="v1")

        with patch("app.platform.prompt.load_prompt", return_value=fake_prompt), \
             patch("app.platform.prompt.loader.load_prompt", return_value=fake_prompt), \
             patch("app.domains.writing.meta.agent.build_writer_model", return_value=MagicMock()), \
             patch("app.domains.writing.meta.agent.create_deep_agent", return_value=MagicMock()), \
             patch("app.domains.writing.meta.agent.compose_skills_backend", return_value=(MagicMock(), [])), \
             patch("app.domains.writing.expert_agent.factory.build_deep_subagent") as mock_deep, \
             patch.object(svc, "_build_evolution_spec", return_value={"name": "evolution"}), \
             patch(
                 "app.domains.writing.expert_agent.agents.interview.build_interview_deep_subagent"
             ) as mock_interview:
            mock_deep.return_value = MagicMock()
            mock_interview.return_value = MagicMock()

            svc._assemble_via_harness(tmp_path)

        # 3 个 deep subagent（storybuilding/detail_outline/writing）
        assert mock_deep.call_count == 3
        deep_names = [c.kwargs["name"] for c in mock_deep.call_args_list]
        assert set(deep_names) == {"storybuilding", "detail-outline", "writing"}

    def test_meta_middleware_includes_goal_and_readonly(self, tmp_path) -> None:
        """harness 装配的 meta middleware 含 Goal + MetaReadOnly（与旧路径一致）。"""
        svc = _make_full_service(tmp_path)
        from app.domains.writing.middleware import GoalMiddleware, MetaReadOnlyMiddleware
        from app.platform.agent.middleware import ErrorRecoveryMiddleware
        fake_prompt = SimpleNamespace(content="PROMPT", version="v1")

        with patch("app.platform.prompt.load_prompt", return_value=fake_prompt), \
             patch("app.platform.prompt.loader.load_prompt", return_value=fake_prompt), \
             patch("app.domains.writing.meta.agent.build_writer_model", return_value=MagicMock()), \
             patch("app.domains.writing.meta.agent.create_deep_agent") as mock_cda, \
             patch("app.domains.writing.meta.agent.compose_skills_backend", return_value=(MagicMock(), [])), \
             patch("app.domains.writing.expert_agent.factory.build_deep_subagent", return_value=MagicMock()), \
             patch.object(svc, "_build_evolution_spec", return_value={"name": "evolution"}), \
             patch(
                 "app.domains.writing.expert_agent.agents.interview.build_interview_deep_subagent",
                 return_value=MagicMock(),
             ):
            svc._assemble_via_harness(tmp_path, "trace-1")

        mw = mock_cda.call_args.kwargs["middleware"]
        mw_types = {type(m) for m in mw}
        assert GoalMiddleware in mw_types
        assert MetaReadOnlyMiddleware in mw_types
        assert ErrorRecoveryMiddleware in mw_types

    def test_trace_middleware_injected_when_trace_id(self, tmp_path) -> None:
        """有 trace_id 时注入 TraceMiddleware（与旧路径 insert(1, Trace) 一致）。"""
        svc = _make_full_service(tmp_path)
        from app.platform.agent.middleware import TraceMiddleware
        fake_prompt = SimpleNamespace(content="PROMPT", version="v1")

        with patch("app.platform.prompt.load_prompt", return_value=fake_prompt), \
             patch("app.platform.prompt.loader.load_prompt", return_value=fake_prompt), \
             patch("app.domains.writing.meta.agent.build_writer_model", return_value=MagicMock()), \
             patch("app.domains.writing.meta.agent.create_deep_agent") as mock_cda, \
             patch("app.domains.writing.meta.agent.compose_skills_backend", return_value=(MagicMock(), [])), \
             patch("app.domains.writing.expert_agent.factory.build_deep_subagent", return_value=MagicMock()), \
             patch.object(svc, "_build_evolution_spec", return_value={"name": "evolution"}), \
             patch(
                 "app.domains.writing.expert_agent.agents.interview.build_interview_deep_subagent",
                 return_value=MagicMock(),
             ):
            svc._assemble_via_harness(tmp_path, "trace-xyz")

        mw = mock_cda.call_args.kwargs["middleware"]
        trace_mw = [m for m in mw if isinstance(m, TraceMiddleware)]
        assert len(trace_mw) == 1

    def test_prompt_version_recorded(self, tmp_path) -> None:
        """装配后记录 prompt 版本（T13，与旧路径一致）。"""
        svc = _make_full_service(tmp_path)
        fake_prompt = SimpleNamespace(content="PROMPT", version="v9")

        with patch("app.platform.prompt.load_prompt", return_value=fake_prompt), \
             patch("app.platform.prompt.loader.load_prompt", return_value=fake_prompt), \
             patch("app.domains.writing.meta.agent.build_writer_model", return_value=MagicMock()), \
             patch("app.domains.writing.meta.agent.create_deep_agent", return_value=MagicMock()), \
             patch("app.domains.writing.meta.agent.compose_skills_backend", return_value=(MagicMock(), [])), \
             patch("app.domains.writing.expert_agent.factory.build_deep_subagent", return_value=MagicMock()), \
             patch.object(svc, "_build_evolution_spec", return_value={"name": "evolution"}), \
             patch(
                 "app.domains.writing.expert_agent.agents.interview.build_interview_deep_subagent",
                 return_value=MagicMock(),
             ):
            svc._assemble_via_harness(tmp_path, "trace-1")
        assert svc._current_prompt_version == "v9"
