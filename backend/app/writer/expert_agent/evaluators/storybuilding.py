"""Storybuilding 统一评估子代理构建器。

评估所有故事维度的跨维度一致性，写入 evaluation.md。
"""
from __future__ import annotations

from pathlib import Path

from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware

from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.writer.expert_agent.types import SubAgentSpec

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "storybuilding_evaluation.md"

_WRITE_PERMISSIONS: list[FilesystemPermission] = [
    FilesystemPermission(operations=["write"], paths=["/evaluation.md"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


def build_storybuilding_evaluator(
    workspace_root: Path,
    middleware: list[AgentMiddleware] | None = None,
    context_file_paths: list[str] | None = None,
) -> SubAgentSpec:
    """构建统一评估子代理规格。

    Args:
        workspace_root:     工作区根目录
        middleware:         额外中间件（可选）
        context_file_paths: 需要注入为评估上下文的文件路径模式列表

    Returns:
        评估子代理规格字典
    """
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8").strip()

    permissions: list[FilesystemPermission] = [
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
    ]
    permissions.extend(_WRITE_PERMISSIONS)

    eval_middleware: list[AgentMiddleware] = []
    if context_file_paths:
        eval_middleware.append(
            ContextAssemblerMiddleware(
                workspace_root,
                file_paths=context_file_paths,
                context_label="评估前置上下文：所有故事维度产物",
            )
        )
    if middleware is not None:
        eval_middleware.extend(middleware)

    return SubAgentSpec(
        name="evaluation-storybuilding",
        system_prompt=system_prompt,
        permissions=permissions,
        middleware=eval_middleware,
    )
