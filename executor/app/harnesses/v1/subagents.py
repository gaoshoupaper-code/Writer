"""v1 各 subagent harness 实现（Phase 1 T1.2）。

每个 SubagentHarness 子类把现有 builder 的装配逻辑包进 build 方法。
装配逻辑零改动（直接调现有 build_*_函数），保证等价性。
"""
from __future__ import annotations

from typing import Any

from app.platform.harness import HarnessContext, SubagentHarness


class StorybuildingHarness(SubagentHarness):
    """storybuilding subagent 的 v1 harness（标准 Deep 版）。"""

    @property
    def name(self) -> str:
        return "storybuilding"

    @property
    def is_deep(self) -> bool:
        return True

    def build_description(self, ctx: HarnessContext) -> str:
        # 复用现有 builder 的 description（等价性）
        from app.domains.writing.expert_agent.agents.storybuilding import (
            build_storybuilding_deep_subagent,
        )
        # description 是 builder 内的字符串，通过调一次 builder 的常量提取不现实，
        # 这里直接等价复制（与 builder 内 description 一致）
        return (
            "适用：需要构建或扩展小说故事世界时调用——包括人物、世界观、"
            "故事核心、故事线（含事件组）。"
            "双层架构：storyline.md 留故事核心+故事线一览表（索引），"
            "每条故事线详情（含事件组）拆到 storyline/S{XX}-{名}.md，一条一个文件。"
            "事件以事件组为单位插入，按三幕式比例编排。"
            "增量迭代：按人物/故事线比值分流两种互斥模式——"
            "人物充足(>3)新增一条故事线，人物不足(≤3)新增一个人物并融入现有故事、不新增故事线；"
            "每次调用只执行一种模式，可循环多次调用。"
            "内置统一评估：产出后调用 evolution 评估跨维度一致性，单次评估修订（仅 1 次）。"
            "委托时必须说明：使用初构还是增量 Skill、本轮焦点、用户扩展方向。"
        )

    def build_system_prompt(self, ctx: HarnessContext) -> str:
        from app.domains.writing.expert_agent.types import apply_style_suffix
        from app.platform.prompt import load_prompt
        return apply_style_suffix(
            load_prompt("storybuilding_system").content.strip(),
            ctx.storybuilding_style,
        )

    def build_middleware(self, ctx: HarnessContext) -> list:
        """注入项 middleware（执行端会先加 PathGuard/Trace/Serialize）。

        storybuilding 额外加 StorylineSingleLineLimit + 可选 ContextAssembler。
        """
        from app.domains.writing.expert_agent.middleware.storyline_single_line_limit import (
            StorylineSingleLineLimitMiddleware,
        )
        mw: list[Any] = [
            StorylineSingleLineLimitMiddleware(ctx.workspace_path, max_new_lines=1),
        ]
        # ContextAssembler 注入 demand.md（与现有 context_file_paths=["demand.md"] 等价）
        from app.platform.agent.middleware import ContextAssemblerMiddleware
        mw.append(ContextAssemblerMiddleware(
            ctx.workspace_path,
            file_paths=["demand.md"],
            context_label="创作需求",
        ))
        return mw

    def build_permissions(self, ctx: HarnessContext) -> list:
        from app.platform.agent.runtime import FilesystemPermission
        return [
            FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
            FilesystemPermission(operations=["write"], paths=["/character/*.md"], mode="allow"),
            FilesystemPermission(operations=["write"], paths=["/worldview.md"], mode="allow"),
            FilesystemPermission(operations=["write"], paths=["/storyline.md"], mode="allow"),
            FilesystemPermission(operations=["write"], paths=["/storyline/*.md"], mode="allow"),
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ]

    def build_skills(self, ctx: HarnessContext) -> list[str]:
        from pathlib import Path
        base = Path(__file__).resolve().parent.parent.parent.parent / "domains" / "writing" / "expert_agent" / "skills"
        return [
            str(base / "storybuilding-initial"),
            str(base / "storybuilding-expand"),
        ]

    def build_deep_params(self, ctx: HarnessContext) -> dict[str, Any]:
        from pathlib import Path
        # evolution evaluator（复用现有 builder）
        from app.domains.writing.expert_agent.evaluators.storybuilding import build_storybuilding_evaluator
        from app.platform.agent.runtime import SubAgent
        # 执行端注入的 middleware_factory 产出的通用 middleware（PathGuard/Trace/Serialize）
        # 由执行端在装配时 prepend 到 build_middleware 结果前。这里只返回 harness 自有的。
        # evolution evaluator 的 middleware 由执行端 middleware_factory 提供，
        # 但 harness 无法直接调 factory，所以 evolution 装配需要执行端协助。
        # 折中：harness 提供 evaluator 的「规格参数」，执行端补 middleware。
        return {
            "system_prompt": self.build_system_prompt(ctx),
            "subagent_middleware": self.build_middleware(ctx),  # harness 自有 middleware
            "skills": self.build_skills(ctx),
            "artifact_paths": [
                ctx.workspace_path / "storyline.md",
                ctx.workspace_path / "storyline",
                ctx.workspace_path / "storyline" / "timeline.md",
            ],
            "max_revisions": 1,
            # evolution_spec 由执行端构建（需 middleware_factory），harness 只标 evaluator 类型
            "evaluator_kind": "storybuilding",
        }


class DetailOutlineHarness(SubagentHarness):
    """detail-outline subagent 的 v1 harness（标准 Deep 版）。"""

    @property
    def name(self) -> str:
        return "detail-outline"

    @property
    def is_deep(self) -> bool:
        return True

    def build_description(self, ctx: HarnessContext) -> str:
        from app.domains.writing.expert_agent.agents.detail_outline import (
            build_detail_outline_deep_subagent,
        )
        return (
            "适用：storybuilding 产出 timeline.md 后，需要把事件编排进章节时调用。"
            "每次处理 timeline 的下一批 5-8 个事件，自主决定分几章、每章几事件，"
            "写入 detail/chapter-XX.md 并增量更新 detail/overview.md。"
            "内置 evolution 评估循环：产出后调用评估子代理，单次评估修订。"
        )

    def build_system_prompt(self, ctx: HarnessContext) -> str:
        from app.domains.writing.expert_agent.types import apply_style_suffix
        from app.platform.prompt import load_prompt
        return apply_style_suffix(
            load_prompt("detail_outline_system").content.strip(),
            ctx.detail_outline_style,
        )

    def build_middleware(self, ctx: HarnessContext) -> list:
        from app.platform.agent.middleware import ContextAssemblerMiddleware
        # 与现有 context_file_paths 等价
        return [ContextAssemblerMiddleware(
            ctx.workspace_path,
            file_paths=[
                "demand.md", "outline.md", "character/*.md", "worldview.md",
                "storyline.md", "storyline/*.md", "detail/overview.md", "detail/chapter-*.md",
            ],
        )]

    def build_permissions(self, ctx: HarnessContext) -> list:
        from app.platform.agent.runtime import FilesystemPermission
        return [
            FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
            FilesystemPermission(operations=["write"], paths=["/detail/**"], mode="allow"),
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ]

    def build_skills(self, ctx: HarnessContext) -> list[str]:
        from pathlib import Path
        base = Path(__file__).resolve().parent.parent.parent.parent / "domains" / "writing" / "expert_agent" / "skills"
        return [str(base / "detail_outline")]

    def build_deep_params(self, ctx: HarnessContext) -> dict[str, Any]:
        return {
            "system_prompt": self.build_system_prompt(ctx),
            "subagent_middleware": self.build_middleware(ctx),
            "skills": self.build_skills(ctx),
            "artifact_paths": [],
            "max_revisions": 1,
            "evaluator_kind": "detail-outline",
        }


class WritingHarness(SubagentHarness):
    """writing subagent 的 v1 harness（标准 Deep 版）。"""

    @property
    def name(self) -> str:
        return "writing"

    @property
    def is_deep(self) -> bool:
        return True

    def build_description(self, ctx: HarnessContext) -> str:
        return (
            "适用：需要生成、追加或修订单个正文章节时调用；不用于大纲、角色或评估。"
            "每次调用只写一个章节（目标约 1000 字，允许 800-1500 字浮动），"
            "写入 chapter/chapter-XX.md。内置 evolution 审查循环：写完后调用审查子代理，"
            "单次审查修订。委托时须提供总章节数、当前章节号、本章目标、必须发生的 beat。"
        )

    def build_system_prompt(self, ctx: HarnessContext) -> str:
        from app.domains.writing.expert_agent.types import apply_style_suffix
        from app.platform.prompt import load_prompt
        return apply_style_suffix(
            load_prompt("writing_system").content.strip(),
            ctx.writing_style,
        )

    def build_middleware(self, ctx: HarnessContext) -> list:
        from app.platform.agent.middleware import ContextAssemblerMiddleware
        return [ContextAssemblerMiddleware(
            ctx.workspace_path,
            file_paths=[
                "demand.md", "outline.md", "character/*.md", "worldview.md",
                "storyline.md", "storyline/*.md", "detail/*.md",
            ],
            context_label="写作前置上下文",
        )]

    def build_permissions(self, ctx: HarnessContext) -> list:
        from app.platform.agent.runtime import FilesystemPermission
        return [
            FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
            FilesystemPermission(operations=["write"], paths=["/chapter/**"], mode="allow"),
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ]

    def build_skills(self, ctx: HarnessContext) -> list[str]:
        from pathlib import Path
        base = Path(__file__).resolve().parent.parent.parent.parent / "domains" / "writing" / "expert_agent" / "skills"
        return [str(base / "writing")]

    def build_deep_params(self, ctx: HarnessContext) -> dict[str, Any]:
        return {
            "system_prompt": self.build_system_prompt(ctx),
            "subagent_middleware": self.build_middleware(ctx),
            "skills": self.build_skills(ctx),
            "artifact_paths": [],
            "max_revisions": 1,
            "evaluator_kind": "writing",
        }


class InterviewHarness(SubagentHarness):
    """interview subagent 的 v1 harness（自定义装配，is_custom=True）。

    interview 不走标准 spec/deep（直接 create_deep_agent + ask_user 工具，
    且移除 ErrorRecovery/PathGuard，换 demand.md 限定）。用 build_compiled 逃生口。
    """

    @property
    def name(self) -> str:
        return "interview"

    @property
    def is_custom(self) -> bool:
        return True

    def build_description(self, ctx: HarnessContext) -> str:
        return (
            "适用：需要与用户多轮对话收集创作需求时调用。"
            "通过 ask_user 工具逐项提问，按 demand.md 模板填充核心/设定/风格/约束四层维度，"
            "维度齐全后请求用户确认成型。产出 demand.md，不挂评估。"
        )

    def build_system_prompt(self, ctx: HarnessContext) -> str:
        from app.platform.prompt import load_prompt
        return load_prompt("interview_system").content.strip()

    def build_middleware(self, ctx: HarnessContext) -> list:
        """interview 特殊：移除 ErrorRecovery/PathGuard，换 demand.md 限定。

        执行端装配时会先注入通用 middleware，然后 harness 的 build_compiled
        会过滤掉 ErrorRecovery/PathGuard 并加 demand.md 限定。
        """
        from app.platform.agent.middleware import FilesystemPathGuardMiddleware
        return [
            FilesystemPathGuardMiddleware(ctx.workspace_path, allowed_write_paths=("/demand.md",))
        ]

    def build_permissions(self, ctx: HarnessContext) -> list:
        # interview 走自定义装配，permissions 由 create_deep_agent 的 backend 控制
        return []

    def build_compiled(self, ctx: HarnessContext, *, assembler: Any | None = None) -> Any:
        """自定义装配：复用现有 build_interview_deep_subagent（等价）。"""
        if assembler is None:
            return None
        from app.domains.writing.expert_agent.agents.interview import build_interview_deep_subagent
        return build_interview_deep_subagent(
            ctx.workspace_path,
            assembler["model"],
            assembler["backend"],
            assembler["middleware_factory"],
        )
