"""统一评估子代理构建器。

将 outline evaluation、detail outline evaluation、writing review、
character evaluation 四个独立评估子代理合并为统一的构建函数。

职责：
  根据 EvaluationType 选择对应的提示词、权限，构建统一的评估子代理规格。
  可选注入 ContextAssemblerMiddleware，由调用方传入文件路径自动读取并注入上下文。

使用方式：
  作为 evolution SubAgent 注册到各子代理的 create_deep_agent 中。

类型映射：
  EvaluationType.OUTLINE        → outline_evaluation.md,  写入 /evaluation.md
  EvaluationType.DETAIL_OUTLINE → detail_outline_evaluation.md, 写入 /detail/evaluation.md
  EvaluationType.WRITING        → review_evaluation.md,   写入 /review/*.md
  EvaluationType.CHARACTER      → character_evaluation.md, 写入 /character/evaluation.md

调用对应关系：
  outline DeepAgent        → build_evaluation_subagent(EvaluationType.OUTLINE, ...)
  detail_outline DeepAgent → build_evaluation_subagent(EvaluationType.DETAIL_OUTLINE, ...)
  writing DeepAgent        → build_evaluation_subagent(EvaluationType.WRITING, ...)
  character DeepAgent      → build_evaluation_subagent(EvaluationType.CHARACTER, ...)
"""
from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import NotRequired, TypedDict

from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware

from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware

# 提示词统一目录（writer/prompt/）
_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompt"


# ---------- 评估类型枚举 ----------

class EvaluationType(StrEnum):
    """评估类型，决定提示词、权限和输出路径。

    调用对应关系：
      EvaluationType.OUTLINE        — outline DeepAgent 评估大纲
      EvaluationType.DETAIL_OUTLINE — detail_outline DeepAgent 评估细纲
      EvaluationType.WRITING        — writing DeepAgent 审查正文
      EvaluationType.CHARACTER      — character DeepAgent 评估角色档案
    """
    OUTLINE = "outline"
    DETAIL_OUTLINE = "detail_outline"
    WRITING = "writing"
    CHARACTER = "character"


# ---------- 子代理规格类型 ----------

class _SubAgentSpec(TypedDict):
    """可运行的子代理规格（内部类型）。"""
    name: str
    system_prompt: str
    permissions: NotRequired[list[FilesystemPermission]]
    middleware: NotRequired[list[AgentMiddleware]]
    response_format: NotRequired[object]


# ---------- 类型配置映射 ----------

# 提示词路径映射
_PROMPT_PATHS: dict[EvaluationType, Path] = {
    EvaluationType.OUTLINE: _PROMPT_DIR / "outline_evaluation.md",
    EvaluationType.DETAIL_OUTLINE: _PROMPT_DIR / "detail_outline_evaluation.md",
    EvaluationType.WRITING: _PROMPT_DIR / "review_evaluation.md",
    EvaluationType.CHARACTER: _PROMPT_DIR / "character_evaluation.md",
}

# 写入权限映射：每种类型只能写入对应的报告文件
_WRITE_PERMISSIONS: dict[EvaluationType, list[FilesystemPermission]] = {
    EvaluationType.OUTLINE: [
        FilesystemPermission(operations=["write"], paths=["/evaluation.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ],
    EvaluationType.DETAIL_OUTLINE: [
        FilesystemPermission(operations=["write"], paths=["/detail/evaluation.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ],
    EvaluationType.WRITING: [
        FilesystemPermission(operations=["write"], paths=["/review/*.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ],
    EvaluationType.CHARACTER: [
        FilesystemPermission(operations=["write"], paths=["/character/evaluation.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ],
}


# ---------- 公共 API ----------

def build_evaluation_subagent(
    evaluation_type: EvaluationType,
    workspace_root: Path,
    middleware: list[AgentMiddleware] | None = None,
    context_file_paths: list[str] | None = None,
) -> _SubAgentSpec:
    """构建统一的评估子代理规格。

    Args:
        evaluation_type:    评估类型（outline / detail_outline / writing / character）
        workspace_root:     工作区根目录
        middleware:         额外中间件列表（可选）
        context_file_paths: 上下文文件路径列表（相对于工作区根目录），
                            传入后创建 ContextAssemblerMiddleware 自动注入文件内容

    Returns:
        评估子代理规格字典，供 _agent_from_subagent_spec 使用
    """
    # 1. 读取提示词
    prompt_path = _PROMPT_PATHS[evaluation_type]
    system_prompt = prompt_path.read_text(encoding="utf-8").strip()

    # 2. 权限：读取所有 + 类型对应的写入权限
    permissions: list[FilesystemPermission] = [
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
    ]
    permissions.extend(_WRITE_PERMISSIONS[evaluation_type])

    # 3. 中间件列表
    eval_middleware: list[AgentMiddleware] = []

    # 4. 可选：ContextAssemblerMiddleware 自动注入文件内容到模型上下文
    if context_file_paths:
        eval_middleware.append(ContextAssemblerMiddleware(
            workspace_root,
            file_paths=context_file_paths,
            context_label="评估前置上下文",
        ))

    # 5. 追加调用方传入的额外中间件
    if middleware is not None:
        eval_middleware.extend(middleware)

    return _SubAgentSpec(
        name=f"evaluation-{evaluation_type}",
        system_prompt=system_prompt,
        permissions=permissions,
        middleware=eval_middleware,
    )
