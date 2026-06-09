"""expert_agent 共享类型与工具函数。"""

from __future__ import annotations

from collections.abc import Callable
from typing import NotRequired, TypedDict

from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware


# ======================================================================
# 共享类型
# ======================================================================

# 中间件工厂函数类型：根据代理名称生成中间件列表
MiddlewareFactory = Callable[[str], list[AgentMiddleware]]


class SubAgentSpec(TypedDict):
    """可运行的子代理规格。

    Fields:
        name:            代理名称
        system_prompt:   系统提示词
        permissions:     文件系统权限列表
        middleware:      额外中间件列表
        response_format: 结构化输出格式（可选）
    """
    name: str
    system_prompt: str
    permissions: NotRequired[list[FilesystemPermission]]
    middleware: NotRequired[list[AgentMiddleware]]
    response_format: NotRequired[object]


# ======================================================================
# 共享工具函数
# ======================================================================


def apply_style_suffix(system_prompt: str, style_suffix: str | None) -> str:
    """将写作风格文本作为 SUFFIX 追加到系统提示词末尾。

    风格注入遵循 DeepAgent 的 SUFFIX 槽位语义：
    系统提示词（USER）在前，风格指导（SUFFIX）在后。
    风格文本紧贴对话历史，模型遵从度最高。
    """
    if not style_suffix:
        return system_prompt
    return f"{system_prompt}\n\n{style_suffix}"
