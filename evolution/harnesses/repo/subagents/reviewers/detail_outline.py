"""细纲审查子代理构建器。

审查 detail/ 下细纲文件的质量，写入 review/detail.md，返回评分和修订建议。
"""
from __future__ import annotations

from pathlib import Path

from langchain.agents.middleware.types import AgentMiddleware

from app.platform.agent.runtime import FilesystemPermission, SubAgentSpec

from app.platform.agent.middleware import ContextAssemblerMiddleware

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "detail_outline_review.md"

_WRITE_PERMISSIONS: list[FilesystemPermission] = [
    FilesystemPermission(operations=["write"], paths=["/review/detail.md"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


def build_detail_outline_reviewer(
    workspace_root: Path,
    middleware: list[AgentMiddleware] | None = None,
    context_file_paths: list[str] | None = None,
) -> SubAgentSpec:
    """构建细纲审查子代理规格。"""
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8").strip()

    permissions: list[FilesystemPermission] = [
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
    ]
    permissions.extend(_WRITE_PERMISSIONS)

    review_middleware: list[AgentMiddleware] = []
    if context_file_paths:
        review_middleware.append(ContextAssemblerMiddleware(
            workspace_root,
            file_paths=context_file_paths,
            context_label="审查前置上下文",
        ))
    if middleware is not None:
        review_middleware.extend(middleware)

    return SubAgentSpec(
        name="review-detail-outline",
        system_prompt=system_prompt,
        permissions=permissions,
        middleware=review_middleware,
    )
