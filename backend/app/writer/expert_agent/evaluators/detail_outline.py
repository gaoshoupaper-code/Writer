"""细纲评估子代理构建器。

评估 detail/ 下细纲文件的质量，写入 detail/evaluation.md，返回评分和修订建议。
"""
from __future__ import annotations

from pathlib import Path

from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware

from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.writer.expert_agent.types import SubAgentSpec

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "detail_outline_evaluation.md"

_WRITE_PERMISSIONS: list[FilesystemPermission] = [
    FilesystemPermission(operations=["write"], paths=["/detail/evaluation.md"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


def build_detail_outline_evaluator(
    workspace_root: Path,
    middleware: list[AgentMiddleware] | None = None,
    context_file_paths: list[str] | None = None,
) -> SubAgentSpec:
    """构建细纲评估子代理规格。"""
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8").strip()

    permissions: list[FilesystemPermission] = [
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
    ]
    permissions.extend(_WRITE_PERMISSIONS)

    eval_middleware: list[AgentMiddleware] = []
    if context_file_paths:
        eval_middleware.append(ContextAssemblerMiddleware(
            workspace_root,
            file_paths=context_file_paths,
            context_label="评估前置上下文",
        ))
    if middleware is not None:
        eval_middleware.extend(middleware)

    return SubAgentSpec(
        name="evaluation-detail-outline",
        system_prompt=system_prompt,
        permissions=permissions,
        middleware=eval_middleware,
    )
