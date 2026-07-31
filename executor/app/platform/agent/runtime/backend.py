"""platform.agent.runtime.backend —— DeepAgents 后端隔离层（PR-08）。

封装 DeepAgents 的 backend 耦合点：FilesystemBackend / CompositeBackend /
compose_skills_backend。未来换框架时只改本文件，领域层无感。

compose_skills_backend 从 writer/expert_agent/factory.py 迁入（纯框架逻辑，
无写作业务依赖）：当 backend 是 virtual_mode FilesystemBackend 时，为 skills
创建 CompositeBackend 路由。

OverwritingFilesystemBackend（DEC-005 方案C / FR-002 / EVD-006）：deepagents
FilesystemBackend.write 默认对已存在文件返回 error（"already exists"），覆盖式产物
（review/*.md、evaluation.md、chapter-XX.md）因此反复触发 read+write 死循环。
本 subclass 在隔离层重写 write：命中"覆盖式产物"路径清单时整体覆盖，其余路径仍走
deepagents 默认的"拒覆盖"语义（EDGE-001）——零改第三方包，领域层通过隔离层 re-export
自动获得覆盖能力。覆盖写逐行保留 0.6.1 的原子写安全语义（O_NOFOLLOW /
O_CREAT|O_TRUNC / newline="" / mkdir parents），见 RSK-001 / NFR-001。
"""

from __future__ import annotations

import os
import re

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import WriteResult

# 覆盖式产物路径清单（OQ-001：从 prompt 固定路径声明抽取，配置化存放便于调整）。
# 这些路径的产物语义是"覆盖式更新"（复查/评估每次跑都会重写同一文件），必须能覆盖；
# 其它路径（如正文 chapter-XX.md 的首次创建）仍保持"拒覆盖"，由 prompt 指引走 edit_file。
# 用虚拟 posix 路径（与 FilesystemPathGuardMiddleware 规范化后的路径一致）fullmatch。
OVERWRITE_PRODUCT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/review/[^/]+\.md$"),        # 各阶段审查报告（storybuilding/detail/chapter）
    re.compile(r"^/evaluation\.md$"),          # 故事构建评估报告（executor 固定路径）
    re.compile(r"^/detail/evaluation\.md$"),   # 细纲评估报告（executor 固定路径）
)


def _is_overwrite_product(virtual_path: str) -> bool:
    """判断虚拟路径是否属于"覆盖式产物"清单（命中即可整体覆盖）。"""
    return any(pattern.fullmatch(virtual_path) for pattern in OVERWRITE_PRODUCT_PATTERNS)


class OverwritingFilesystemBackend(FilesystemBackend):
    """覆盖式 FilesystemBackend：对覆盖式产物路径整体覆盖，其余保持拒覆盖（方案C）。

    deepagents 0.6.1 的 write 对目标已存在即 return error，第三方包内无 overwrite
    参数（EVD-006）。本类在 agent 可达的隔离层重写 write：命中 OVERWRITE_PRODUCT_PATTERNS
    的路径跳过 exists 守卫走覆盖写，其余路径委托 super().write() 保留默认"拒覆盖"语义
    （EDGE-001）。覆盖写与新建写共用 0.6.1 的原子写路径（O_NOFOLLOW + O_CREAT|O_TRUNC
    + newline="" + mkdir），见 RSK-001 / NFR-001。

    skills 只读目录走 CompositeBackend 独立路由（仍是普通 FilesystemBackend），
    覆盖语义不被波及（EDGE-005 / 兼容性）。
    """

    def write(self, file_path: str, content: str) -> WriteResult:  # noqa: D401
        """对覆盖式产物路径整体覆盖，其余路径走 deepagents 默认（拒覆盖）语义。"""
        # 非覆盖式产物直接委托父类，保持默认"已存在即报错"语义（EDGE-001）。
        if not _is_overwrite_product(file_path):
            return super().write(file_path, content)

        try:
            resolved_path = self._resolve_path(file_path)
        except (OSError, RuntimeError) as e:
            return WriteResult(error=f"Error writing file '{file_path}': {e}")

        try:
            # Create parent directories if needed（覆盖与新建一致）
            resolved_path.parent.mkdir(parents=True, exist_ok=True)

            # Prefer O_NOFOLLOW to avoid writing through symlinks（逐行保留 0.6.1 安全语义）
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(resolved_path, flags, 0o644)
            # newline="" disables Windows CRLF translation so callers that
            # pass LF-only content get LF-only bytes on disk.
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(content)

            return WriteResult(path=file_path)
        except (OSError, UnicodeEncodeError) as e:
            return WriteResult(error=f"Error writing file '{file_path}': {e}")


def compose_skills_backend(
    backend: object,
    skill_paths: list[str],
) -> tuple[object, list[str]]:
    """当 backend 是 virtual_mode FilesystemBackend 时，为 skills 创建路由。

    FilesystemBackend(virtual_mode=True) 要求所有路径是相对于 root_dir 的虚拟路径，
    无法解析 Windows 绝对路径。skills 目录是应用代码的一部分，不在 workspace 内。

    解决方案：用 CompositeBackend 将 skills 前缀路由到独立的 FilesystemBackend，
    workspace 操作走默认 backend，互不干扰。

    skills 路由仍用普通 FilesystemBackend（只读目录，不应被覆盖语义波及，EDGE-005）。

    Args:
        backend:     原始 backend（通常是 virtual_mode OverwritingFilesystemBackend）
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
        # virtual_mode=True 让 ls("/") 列出 skill_dir 内容；skills 是只读资源，
        # 用普通 FilesystemBackend（保留拒覆盖语义，不被 overwrite 波及）。
        routes[prefix] = FilesystemBackend(root_dir=skill_dir, virtual_mode=True)
        virtual_sources.append(prefix)

    composite = CompositeBackend(default=backend, routes=routes)
    return composite, virtual_sources


__all__ = [
    "CompositeBackend",
    "FilesystemBackend",
    "OVERWRITE_PRODUCT_PATTERNS",
    "OverwritingFilesystemBackend",
    "compose_skills_backend",
]
