"""harness 基类契约定义（Phase 1 T1.1，D16 契约化 Python）。

核心思想（D4 任意代码 × D16 契约化的调和点）：
  proposer 在 build 方法体内写任意 Python（可定义新 middleware 类并实例化），
  但返回值必须符合签名类型。静态检查（D10）校验方法签名 + 返回类型 + import。

两个抽象基类：
  - WriterHarness: 顶层 meta agent 的装配契约
  - SubagentHarness: 单个 subagent 的装配契约（支持普通 SubAgent 规格 + Deep 版）

HarnessContext（S5）封装请求级运行时上下文，作为 build 方法参数传入。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from langchain.agents.middleware.types import AgentMiddleware


@dataclass
class HarnessContext:
    """请求级运行时上下文（S5：build 方法参数传入）。

    harness 实例无状态可复用，每次 build 传入新 ctx。
    含装配 agent 所需的全部请求级信息：workspace/trace/owner/style。
    """

    workspace_path: Path
    trace_id: str | None = None
    owner_id: str | None = None
    workspace_id: str | None = None
    # 风格相关（meta 风格 + 各 subagent 风格 suffix）
    meta_style: str | None = None
    storybuilding_style: str | None = None
    detail_outline_style: str | None = None
    writing_style: str | None = None


class WriterHarness(ABC):
    """顶层 meta agent 的装配契约（S4）。

    proposer 通过实现这些 build 方法定义 meta agent 的：
    - system_prompt（含风格注入）
    - skills（SKILL.md 目录路径）
    - middleware（meta 层中间件栈，如 Goal/ErrorRecovery/MetaReadOnly）
    - tools（meta 层工具，通常为空）
    - subagents（注册哪些子代理）

    model/backend/checkpointer 由执行端管（多用户隔离 + workspace），不归 harness。
    """

    @abstractmethod
    def build_system_prompt(self, ctx: HarnessContext) -> str:
        """meta agent 的系统提示词（可注入风格）。"""

    @abstractmethod
    def build_skills(self, ctx: HarnessContext) -> list[str]:
        """meta 层 SKILL.md 目录路径列表。"""

    @abstractmethod
    def build_middleware(self, ctx: HarnessContext) -> list[AgentMiddleware]:
        """meta 层中间件栈。"""

    def build_tools(self, ctx: HarnessContext) -> list[Any]:
        """meta 层工具（默认空，可覆盖）。"""
        return []

    @abstractmethod
    def build_subagents(self, ctx: HarnessContext) -> list[Any]:
        """注册的子代理列表（CompiledSubAgent 或 SubAgent 规格）。

        返回的是已构建好的 subagent 实例/规格，由 harness 实现负责装配
        （内部调对应 SubagentHarness.build_*）。
        """

    def harness_id(self) -> str:
        """harness 版本标识（供 trace 记录 + 版本管理）。默认用类名。"""
        return self.__class__.__name__


class SubagentHarness(ABC):
    """单个 subagent 的装配契约（S4）。

    支持两种形态：
    - 普通 SubAgent 规格（如 general-purpose/interview）：实现 build_spec()
    - Deep 版（含 evolution 评估器，如 storybuilding/detail_outline/writing）：
      实现 build_deep_params()，返回 build_deep_subagent 所需参数

    默认 is_deep=False（普通规格），deep 子代理覆盖 is_deep=True。
    """

    @property
    def name(self) -> str:
        """子代理名称（如 storybuilding/writing）。"""
        raise NotImplementedError

    @property
    def is_deep(self) -> bool:
        """是否为 Deep 版（含 evolution 评估循环）。默认 False。"""
        return False

    @abstractmethod
    def build_description(self, ctx: HarnessContext) -> str:
        """子代理功能描述（供父代理选择委托目标）。"""

    @abstractmethod
    def build_system_prompt(self, ctx: HarnessContext) -> str:
        """子代理系统提示词（可注入风格 suffix）。"""

    @abstractmethod
    def build_middleware(self, ctx: HarnessContext) -> list[AgentMiddleware]:
        """子代理的额外中间件（注入项，RevisionLimit/ArtifactValidation 由执行端统一加）。"""

    @abstractmethod
    def build_permissions(self, ctx: HarnessContext) -> list[Any]:
        """文件系统权限规则（FilesystemPermission 列表）。"""

    def build_skills(self, ctx: HarnessContext) -> list[str]:
        """SKILL.md 目录路径列表（默认空，可覆盖）。"""
        return []

    def build_spec(self, ctx: HarnessContext) -> dict[str, Any]:
        """普通 SubAgent 规格 dict（is_deep=False 时用）。

        默认实现组装 name/description/system_prompt/permissions/middleware。
        proposer 通常不需覆盖，除非要改规格结构。
        """
        return {
            "name": self.name,
            "description": self.build_description(ctx),
            "system_prompt": self.build_system_prompt(ctx),
            "permissions": self.build_permissions(ctx),
            "middleware": self.build_middleware(ctx),
        }

    def build_deep_params(self, ctx: HarnessContext) -> dict[str, Any]:
        """Deep 版参数（is_deep=True 时用，传给 build_deep_subagent）。

        返回 dict 含：system_prompt / subagent_middleware / skills /
        artifact_paths / max_revisions / evolution_spec。
        description 由 build_description 单独提供（CompiledSubAgent 用）。

        proposer 实现 deep 子代理时必须覆盖此方法 + is_deep=True。
        """
        raise NotImplementedError(
            f"{self.name} 未实现 build_deep_params（is_deep=True 必须实现）"
        )

    def build_compiled(
        self, ctx: HarnessContext, *, assembler: Any | None = None
    ) -> Any:
        """自定义装配逃生口（is_custom=True 时用）。

        个别 subagent 不走标准 spec/deep 路径（如 interview 直接 create_deep_agent
        + ask_user 工具），覆盖此方法做完全自定义装配。

        assembler: 执行端注入的装配器（提供 model/backend/middleware_factory 等），
        harness 用它调底层工厂，不直接持有这些基础设施。

        默认返回 None，表示走标准 spec/deep 路径。
        """
        return None

    @property
    def is_custom(self) -> bool:
        """是否为完全自定义装配（覆盖 build_compiled）。默认 False。"""
        return False
