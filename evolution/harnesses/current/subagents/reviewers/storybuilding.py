"""Storybuilding 统一审查子代理构建器。

审查所有故事维度的跨维度一致性，写入 review/storybuilding.md。
审查器自主读取所有文件，不依赖 ContextAssemblerMiddleware。
"""
from __future__ import annotations

from pathlib import Path

from langchain.agents.middleware.types import AgentMiddleware

from app.platform.agent.runtime import FilesystemPermission, SubAgentSpec

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "storybuilding_review.md"

_WRITE_PERMISSIONS: list[FilesystemPermission] = [
    FilesystemPermission(operations=["write"], paths=["/review/storybuilding.md"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


def build_storybuilding_reviewer(
    workspace_root: Path,
    middleware: list[AgentMiddleware] | None = None,
) -> SubAgentSpec:
    """构建统一审查子代理规格。

    审查器自主读取所有文件，不使用 ContextAssemblerMiddleware。
    system prompt 中已列出需要读取的文件清单。

    Args:
        workspace_root: 工作区根目录
        middleware:     额外中间件（可选）

    Returns:
        审查子代理规格字典
    """
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8").strip()

    permissions: list[FilesystemPermission] = [
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
    ]
    permissions.extend(_WRITE_PERMISSIONS)

    review_middleware: list[AgentMiddleware] = []
    if middleware is not None:
        review_middleware.extend(middleware)

    return SubAgentSpec(
        name="review-storybuilding",
        system_prompt=system_prompt,
        permissions=permissions,
        middleware=review_middleware,
    )
