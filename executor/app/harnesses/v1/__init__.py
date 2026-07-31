"""harness v1：现有写作 agent 的契约化包装（Phase 1 T1.2）。

这是「初始 harness 版本」——把现有 meta agent + 5 subagent 的装配逻辑原样
搬进 harness build 方法。装配逻辑零改动（复用现有 build_*_subagent /
build_deep_subagent / build_*_evaluator 函数），等价性天然保证。

设计意图：
  - v1 是 baseline，git 提交后作为进化起点
  - proposer 后续改 harness 时，改的是这些 build 方法内部的逻辑
  - 装配的实际执行（调 create_deep_agent）仍由执行端做（控制权保留）

非标准 subagent：
  - general-purpose: 用 GENERAL_PURPOSE_SUBAGENT（普通 SubAgent 规格）
  - interview: 直接 create_deep_agent + ask_user（自定义装配，is_custom=True）
  - storybuilding/detail_outline/writing: 标准 Deep 版（is_deep=True）

设计依据：设计文档 S4/S5 + 迁移边界（executors 文件去留）。
"""
from __future__ import annotations

from app.platform.harness import HarnessContext, SubagentHarness, WriterHarness

# subagent harness 实现单独导入（避免循环）
from app.harnesses.v1.subagents import (
    DetailOutlineHarness,
    InterviewHarness,
    StorybuildingHarness,
    WritingHarness,
)

__all__ = ["WriterHarnessV1", "HarnessContext"]


class WriterHarnessV1(WriterHarness):
    """meta agent 的 v1 harness 实现（现有行为等价包装）。

    build 方法内部复用现有 MetaAgentService 的装配逻辑：
      - build_system_prompt: load_prompt("meta_system") + 风格注入
      - build_skills: 2 个 meta skill 路径
      - build_middleware: Goal + ErrorRecovery + MetaReadOnly (+Trace)
      - build_subagents: 5 个 SubagentHarness 的装配
    """

    def __init__(self) -> None:
        # 5 个 subagent harness 实例（无状态，可复用）
        self._subagent_harnesses: list[SubagentHarness] = [
            InterviewHarness(),
            StorybuildingHarness(),
            DetailOutlineHarness(),
            WritingHarness(),
        ]

    def build_system_prompt(self, ctx: HarnessContext) -> str:
        """meta prompt：从 load_prompt 拉 + 风格注入。"""
        from app.platform.prompt import load_prompt
        prompt = load_prompt("meta_system").content
        if ctx.meta_style:
            prompt = f"{prompt}\n\n---\n【主控风格】\n{ctx.meta_style}\n---"
        return prompt

    def build_skills(self, ctx: HarnessContext) -> list[str]:
        """meta 层 2 个 skill 路径（auto-pipeline + interactive-gating）。"""
        from pathlib import Path
        meta_skills_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "domains" / "writing" / "meta" / "skills"
        )
        return [
            str(meta_skills_dir / "auto-pipeline"),
            str(meta_skills_dir / "interactive-gating"),
        ]

    def build_middleware(self, ctx: HarnessContext) -> list:
        """meta middleware：Goal + ErrorRecovery + MetaReadOnly (+Trace)。

        Trace middleware 需要 trace_recorder，由执行端注入（这里先返回不含 Trace 的基础栈，
        执行端按需插入）。实际等价于现有 _agent_for_workspace 的 middleware 列表。
        """
        from app.domains.writing.middleware import GoalMiddleware, MetaReadOnlyMiddleware
        from app.platform.agent.middleware import ErrorRecoveryMiddleware
        # 注意：Trace middleware 需 trace_recorder + trace_id，执行端注入
        # 这里返回基础栈，与现有 [Goal, ErrorRecovery, MetaReadOnly] 一致
        return [
            GoalMiddleware(),
            ErrorRecoveryMiddleware(),
            MetaReadOnlyMiddleware(),
        ]

    def build_tools(self, ctx: HarnessContext) -> list:
        """meta 工具：空（现有行为）。"""
        return []

    def build_subagents(self, ctx: HarnessContext) -> list:
        """返回 subagent harness 实例列表（由执行端逐个装配）。

        返回 harness 实例而非已装配的 CompiledSubAgent，因为装配需要
        model/backend/middleware_factory（执行端基础设施）。执行端拿到
        harness 列表后，按 is_deep/is_custom 分别装配。
        """
        # general-purpose 不走 harness（用框架内置 GENERAL_PURPOSE_SUBAGENT），
        # 由执行端单独处理。这里只返回 4 个有 harness 的 subagent。
        return list(self._subagent_harnesses)

    def harness_id(self) -> str:
        return "writer-harness-v1"
