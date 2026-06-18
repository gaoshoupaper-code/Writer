"""正文审查评估子代理构建器。

审查正文章节质量，写入 review/ 下对应文件，返回审查结论和修订建议。
"""
from __future__ import annotations

from pathlib import Path

from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware

from app.platform.agent.middleware import ContextAssemblerMiddleware
from app.writer.expert_agent.types import SubAgentSpec

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "writing_evaluation.md"

_WRITE_PERMISSIONS: list[FilesystemPermission] = [
    FilesystemPermission(operations=["write"], paths=["/review/*.md"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


def build_writing_evaluator(
    workspace_root: Path,
    middleware: list[AgentMiddleware] | None = None,
    context_file_paths: list[str] | None = None,
) -> SubAgentSpec:
    """构建正文审查评估子代理规格。"""
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
        name="evaluation-writing",
        system_prompt=system_prompt,
        permissions=permissions,
        middleware=eval_middleware,
    )
