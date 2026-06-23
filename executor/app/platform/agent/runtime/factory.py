"""platform.agent.runtime.factory —— DeepAgents agent 工厂隔离层（PR-08）。

re-export create_deep_agent（DeepAgents 的核心 agent 构建函数），让领域层
从这里 import 而非直接碰 deepagents。未来换框架时只改本文件。

注意：写作专属的装配逻辑（build_deep_subagent，含 evolution subagent +
RevisionLimitMiddleware + ArtifactValidationMiddleware）仍在
writer/expert_agent/factory.py——那是领域逻辑，不属于 runtime 隔离层。
"""

from __future__ import annotations

# DeepAgents agent 构建函数 re-export（隔离层出口）
from deepagents import create_deep_agent
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

__all__ = [
    "GENERAL_PURPOSE_SUBAGENT",
    "create_deep_agent",
]
