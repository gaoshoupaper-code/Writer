"""platform.agent.runtime.backend —— DeepAgents 后端隔离层（PR-08）。

封装 DeepAgents 的 backend 耦合点：FilesystemBackend / CompositeBackend /
compose_skills_backend。未来换框架时只改本文件，领域层无感。

compose_skills_backend 从 writer/expert_agent/factory.py 迁入（纯框架逻辑，
无写作业务依赖）：当 backend 是 virtual_mode FilesystemBackend 时，为 skills
创建 CompositeBackend 路由。
"""

from __future__ import annotations

# DeepAgents backend 类型 re-export（隔离层：领域层从这里 import，不直接碰 deepagents）
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend


def compose_skills_backend(
    backend: object,
    skill_paths: list[str],
) -> tuple[object, list[str]]:
    """当 backend 是 virtual_mode FilesystemBackend 时，为 skills 创建路由。

    FilesystemBackend(virtual_mode=True) 要求所有路径是相对于 root_dir 的虚拟路径，
    无法解析 Windows 绝对路径。skills 目录是应用代码的一部分，不在 workspace 内。

    解决方案：用 CompositeBackend 将 skills 前缀路由到独立的 FilesystemBackend，
    workspace 操作走默认 backend，互不干扰。

    Args:
        backend:     原始 backend（通常是 virtual_mode FilesystemBackend）
        skill_paths: skills 目录的绝对文件系统路径列表

    Returns:
        (effective_backend, virtual_skill_sources) 元组
    """
    is_virtual_fs = (
        isinstance(backend, FilesystemBackend)
        and getattr(backend, "virtual_mode", False)
    )
    # 非 virtual backend 可以直接用绝对路径，无需 CompositeBackend
    if not is_virtual_fs:
        return backend, skill_paths

    routes: dict[str, FilesystemBackend] = {}
    virtual_sources: list[str] = []

    for i, skill_dir in enumerate(skill_paths):
        prefix = f"/_skills_{i}/"
        # virtual_mode=True 让 ls("/") 列出 skill_dir 内容
        routes[prefix] = FilesystemBackend(root_dir=skill_dir, virtual_mode=True)
        virtual_sources.append(prefix)

    composite = CompositeBackend(default=backend, routes=routes)
    return composite, virtual_sources


__all__ = [
    "CompositeBackend",
    "FilesystemBackend",
    "compose_skills_backend",
]
