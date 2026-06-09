"""Detail Outline 子代理 — 基于 DeepAgent 的细纲生成子代理。

架构概览：
  本模块构建 detail-outline 子代理，用于将大纲拆解为逐章细纲文件。
  采用 DeepAgent 架构，子代理自主完成"生成 → 评估 → 修订"循环。

  每次调用只处理一个文件（overview 或单个章节），由主代理控制章节推进节奏。

核心组件：
  - build_detail_outline_subagent():          构建细纲子代理规格（供 deep 子代理复用）
  - build_detail_outline_deep_subagent():     构建基于 DeepAgent 的完整子代理（含 evolution 评估循环）

  - ContextAssemblerMiddleware: 细纲子代理的上下文组装中间件
    在每个新轮次开始时，从文件系统读取指定文件并注入。
"""

from __future__ import annotations

from pathlib import Path
from typing import NotRequired, TypedDict

from deepagents import CompiledSubAgent, SubAgent
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.writer.subagents.deep_subagent_factory import build_deep_subagent
from app.writer.subagents.outline_subagent import MiddlewareFactory
from app.writer.subagents.evaluation_subagent import EvaluationType, build_evaluation_subagent

# 细纲子代理的系统提示词文件路径（统一存放在 writer/prompt/ 目录）
DETAIL_OUTLINE_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "prompt" / "detail_outline_system_prompt.md"
)


def _apply_style_suffix(system_prompt: str, style_suffix: str | None) -> str:
    """将写作风格文本作为 SUFFIX 追加到系统提示词末尾。"""
    if not style_suffix:
        return system_prompt
    return f"{system_prompt}\n\n{style_suffix}"


# ---------------------------------------------------------------------------
# 状态类型
# ---------------------------------------------------------------------------


class _SubAgentSpec(TypedDict):
    """可运行的子代理规格（内部类型）。"""
    name: str
    system_prompt: str
    permissions: NotRequired[list[FilesystemPermission]]
    middleware: NotRequired[list[AgentMiddleware]]
    response_format: NotRequired[object]


# ---------------------------------------------------------------------------
# 子代理规格构建器
# ---------------------------------------------------------------------------


def build_detail_outline_subagent(
    middleware: list[AgentMiddleware] | None = None,
    style_suffix: str | None = None,
) -> _SubAgentSpec:
    """构建细纲子代理规格。

    权限配置：
    - 读取：允许读取所有文件（/**）
    - 写入：只允许写入 /detail/**（细纲文件）
    - 拒绝：禁止写入其他所有文件

    Args:
        middleware:     额外中间件列表（可选）
        style_suffix:  细纲风格 SUFFIX 文本（可选）

    Returns:
        子代理规格字典
    """
    system_prompt = _apply_style_suffix(DETAIL_OUTLINE_PROMPT_PATH.read_text(encoding="utf-8").strip(), style_suffix)
    permissions = [
        FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/detail/**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]
    spec = _SubAgentSpec(
        name="detail-outline",
        system_prompt=system_prompt,
        permissions=permissions,
    )
    if middleware is not None:
        spec["middleware"] = middleware
    return spec


def build_detail_outline_deep_subagent(
    workspace_root: Path,
    model: BaseChatModel,
    backend: BackendProtocol,
    middleware_factory: MiddlewareFactory,
    style_suffix: str | None = None,
    context_file_paths: list[str] | None = None,
) -> CompiledSubAgent:
    """构建基于 DeepAgent 的 detail-outline 子代理（内含 evolution 评估循环）。

    替代 build_detail_outline_pipeline_subagent 的 StateGraph 管道模式。
    子代理自主决策：生成 → 调用 evolution 评估 → 根据反馈修订（最多 3 轮）。

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:   中间件工厂函数
        style_suffix:        细纲风格 SUFFIX 文本（可选）
        context_file_paths:  上下文文件路径列表（相对于工作区根目录）

    Returns:
        编译后的子代理字典 {name, description, runnable}
    """
    # ---- 主代理 system prompt + middleware ----
    project_middleware = list(middleware_factory("detail-outline-subagent"))
    project_middleware.append(ContextAssemblerMiddleware(
        workspace_root,
        file_paths=context_file_paths or [],
    ))
    primary_spec = build_detail_outline_subagent(project_middleware, style_suffix)

    # ---- evolution 子代理规格 ----
    evaluation_spec = build_evaluation_subagent(
        EvaluationType.DETAIL_OUTLINE,
        workspace_root,
        middleware_factory("detail-outline-evaluation-subagent"),
        context_file_paths=["outline.md", "character/*.md", "detail/*.md"],
    )

    # ---- 构建 evolution SubAgent dict ----
    evolution = SubAgent(
        name="evolution",
        description="评估 detail/ 下细纲文件的质量，写入 detail/evaluation.md，返回评分和修订建议。",
        system_prompt=evaluation_spec["system_prompt"],
        permissions=evaluation_spec.get("permissions"),
        middleware=evaluation_spec.get("middleware"),
    )

    # ---- 组装 system prompt ----
    base_prompt = primary_spec["system_prompt"]
    if "评估机制" in base_prompt:
        base_prompt = base_prompt.split("评估机制")[0].rstrip()
    evolution_suffix = (
        "评估机制（evolution 子代理）：\n"
        "- 你有一个名为 \"evolution\" 的子代理，用于评估你的创作产物质量。\n"
        "- 工作流程：完成 detail/ 下文件写入后，调用 evolution 子代理评估质量。\n"
        "- evolution 会读取 detail/ 下的文件并写入评估报告到 detail/evaluation.md，然后返回评分和修改建议。\n"
        "- 如果 evolution 返回\"建议修改\"或\"必须修改\"，你**必须**读取 detail/evaluation.md 中的详细评估报告，"
        "根据核心问题和修改建议修订当前文件，然后再次调用 evolution 评估修订后的版本。\n"
        "- 如果 evolution 返回\"无需修改\"，直接向父代理返回结果。\n"
        "- 最多调用 evolution 3 次（含首次评估），超过后系统会强制终止评估循环。\n"
        "- 返回父代理时，请在回复中包含：修订轮数、是否有质量风险。"
    )
    system_prompt = f"{base_prompt}\n\n{evolution_suffix}"

    # ---- 调用工厂 ----
    return build_deep_subagent(
        name="detail-outline",
        description=(
            "适用：outline.md 通过 evaluation 后，需要将大纲拆解为逐章细纲时调用。"
            "每次调用只生成一个细纲文件（overview.md 或 chapter-XX.md），"
            "内置 evolution 评估循环：生成后自动评估质量，如果评估建议修订会自动修订，最多 3 轮。"
            "主代理控制章节推进节奏：先调用生成 overview，获取总章节数后逐章调用。"
            "委托时请说明本次要生成的文件（overview 或具体章节）和创作目标。"
        ),
        model=model,
        system_prompt=system_prompt,
        evolution_spec=evolution,
        subagent_middleware=primary_spec.get("middleware"),
        backend=backend,
        max_revisions=3,
    )
