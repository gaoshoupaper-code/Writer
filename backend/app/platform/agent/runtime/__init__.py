"""platform.agent.runtime —— DeepAgents 薄隔离层（PR-08）。

把 DeepAgents 框架的 5 个核心耦合点收敛到此包，领域层（meta/image/character/
expert_agent）只 import 本包，不直接 import deepagents。未来 6 个月内若换框架，
只改本包的 re-export，领域层无感。

收敛的耦合点：
- ``create_deep_agent``（runtime.factory）
- ``CompiledSubAgent`` / ``SubAgent`` / ``SubAgentSpec`` / ``AgentMiddleware`` /
  ``FilesystemPermission`` / ``BackendProtocol``（runtime.types）
- ``FilesystemBackend`` / ``CompositeBackend`` / ``compose_skills_backend``（runtime.backend）
- ``GENERAL_PURPOSE_SUBAGENT``（runtime.factory）

写作专属装配（build_deep_subagent + evolution + RevisionLimit）仍在
writer/expert_agent/，不在本包。
"""

from app.platform.agent.runtime.backend import (
    CompositeBackend,
    FilesystemBackend,
    compose_skills_backend,
)
from app.platform.agent.runtime.factory import (
    GENERAL_PURPOSE_SUBAGENT,
    create_deep_agent,
)
from app.platform.agent.runtime.types import (
    AgentMiddleware,
    BackendProtocol,
    CompiledSubAgent,
    FilesystemPermission,
    MiddlewareFactory,
    SubAgent,
    SubAgentSpec,
)

__all__ = [
    "AgentMiddleware",
    "BackendProtocol",
    "CompiledSubAgent",
    "CompositeBackend",
    "FilesystemBackend",
    "FilesystemPermission",
    "GENERAL_PURPOSE_SUBAGENT",
    "MiddlewareFactory",
    "SubAgent",
    "SubAgentSpec",
    "compose_skills_backend",
    "create_deep_agent",
]
