"""platform.agent.runtime.types —— DeepAgents 类型隔离层（PR-08）。

re-export DeepAgents 的核心类型符号，让领域层从这里 import 而非直接碰 deepagents。
未来换框架时只改本文件。

SubAgentSpec / MiddlewareFactory 从 writer/expert_agent/types.py 迁入
（框架级类型，领域无关）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NotRequired, TypedDict

# DeepAgents / LangChain 类型 re-export（隔离层出口）
from deepagents import CompiledSubAgent, SubAgent
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware


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


__all__ = [
    "AgentMiddleware",
    "BackendProtocol",
    "CompiledSubAgent",
    "FilesystemPermission",
    "MiddlewareFactory",
    "SubAgent",
    "SubAgentSpec",
]
