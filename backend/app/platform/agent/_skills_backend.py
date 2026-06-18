"""platform.agent._skills_backend 门面（PR-02 过渡层）。

_compose_skills_backend 实体仍在 writer/expert_agent/factory.py，
本文件 re-export 指向它，让 image domain 不再直接 import writer（PR-02 目标）。

实体迁移在 PR-08 完成：随 agent_runtime 薄隔离层一起迁入 platform/agent/runtime/。

transitional re-export —— PR-08 实体迁入后删除
"""

from app.writer.expert_agent.factory import _compose_skills_backend

__all__ = ["_compose_skills_backend"]
