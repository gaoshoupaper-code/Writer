"""Storybuilding 统一评估子代理构建器。

评估所有故事维度的跨维度一致性，写入 evaluation.md。
评估器自主读取所有文件，不依赖 ContextAssemblerMiddleware。
"""
from __future__ import annotations

from pathlib import Path

from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware

from app.writer.expert_agent.types import SubAgentSpec

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "storybuilding_evaluation.md"

_WRITE_PERMISSIONS: list[FilesystemPermission] = [
    FilesystemPermission(operations=["write"], paths=["/evaluation.md"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


def build_storybuilding_evaluator(
    workspace_root: Path,
    middleware: list[AgentMiddleware] | None = None,
) -> SubAgentSpec:
    """构建统一评估子代理规格。

    评估器自主读取所有文件，不使用 ContextAssemblerMiddleware。
    system prompt 中已列出需要读取的文件清单。

    Args:
        workspace_root: 工作区根目录
        middleware:     额外中间件（可选）

    Returns:
        评估子代理规格字典
    """
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8").strip()

    permissions: list[FilesystemPermission] = [
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
    ]
    permissions.extend(_WRITE_PERMISSIONS)

    eval_middleware: list[AgentMiddleware] = []
    if middleware is not None:
        eval_middleware.extend(middleware)

    return SubAgentSpec(
        name="evaluation-storybuilding",
        system_prompt=system_prompt,
        permissions=permissions,
        middleware=eval_middleware,
    )
