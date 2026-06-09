"""Outline 子代理 — 大纲生成 + evolution 评估循环。

架构概览：
  本模块将 outline 子代理构建为 DeepAgent（create_deep_agent），
  内部注册 evolution SubAgent 进行评估：
  1. 主代理（outline）：生成或修订 outline.md
  2. evolution 子代理：评估 outline.md 质量，写入 evaluation.md
  3. 自主修订循环：evolution 建议修订时，主代理自动修订（最多 3 轮）

导出的公共 API：
  - build_outline_subagent():        构建单独的 outline 子代理规格
  - build_outline_deep_subagent():    构建 DeepAgent 版本（含 evolution 评估循环）

导出的共享类型（供 detail_outline / writing 模块复用）：
  - MiddlewareFactory: 中间件工厂函数类型别名
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NotRequired, TypedDict

from deepagents import CompiledSubAgent, SubAgent
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel

from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.writer.subagents.deep_subagent_factory import build_deep_subagent
from app.writer.subagents.evaluation_subagent import EvaluationType, build_evaluation_subagent

# 大纲子代理的系统提示词文件路径（统一存放在 writer/prompt/ 目录）
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompt" / "outline_system_prompt.md"


# ======================================================================
# 共享类型
# ======================================================================

# 中间件工厂函数类型：根据代理名称生成中间件列表
MiddlewareFactory = Callable[[str], list[AgentMiddleware]]


# ======================================================================
# 内部类型
# ======================================================================


class _RunnableSubAgentSpec(TypedDict):
    """可运行的子代理规格（内部类型）。

    Fields:
        name:           代理名称
        system_prompt:  系统提示词
        permissions:    文件系统权限列表
        middleware:     额外中间件列表
        response_format: 结构化输出格式（可选）
    """
    name: str
    system_prompt: str
    permissions: NotRequired[list[FilesystemPermission]]
    middleware: NotRequired[list[AgentMiddleware]]
    response_format: NotRequired[object]


def _apply_style_suffix(system_prompt: str, style_suffix: str | None) -> str:
    """将写作风格文本作为 SUFFIX 追加到系统提示词末尾。

    风格注入遵循 DeepAgent 的 SUFFIX 槽位语义：
    系统提示词（USER）在前，风格指导（SUFFIX）在后。
    风格文本紧贴对话历史，模型遵从度最高。

    如果 style_suffix 为空则不做任何修改。
    """
    if not style_suffix:
        return system_prompt
    return f"{system_prompt}\n\n{style_suffix}"


# ======================================================================
# 子代理构建函数
# ======================================================================


def build_outline_subagent(middleware: list[AgentMiddleware] | None = None, style_suffix: str | None = None) -> _RunnableSubAgentSpec:
    """构建单独的 outline 子代理规格。

    权限配置：
    - 读取：允许读取所有文件（/**）
    - 写入：只允许写入 /outline.md
    - 拒绝：禁止写入其他所有文件

    Args:
        middleware:     额外中间件列表（可选）
        style_suffix:  大纲风格 SUFFIX 文本（可选，追加到系统提示词末尾）

    Returns:
        子代理规格字典，供 build_outline_deep_subagent 使用
    """
    system_prompt = _apply_style_suffix(PROMPT_PATH.read_text(encoding="utf-8").strip(), style_suffix)

    permissions = [
        FilesystemPermission(
            operations=["read"],
            paths=["/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/outline.md"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/**"],
            mode="deny",
        ),
    ]

    spec = _RunnableSubAgentSpec(
        name="outline",
        system_prompt=system_prompt,
        permissions=permissions,
    )
    if middleware is not None:
        spec["middleware"] = middleware
    return spec


def build_outline_deep_subagent(
    workspace_root: Path,
    model: BaseChatModel,
    backend: BackendProtocol,
    middleware_factory: MiddlewareFactory,
    style_suffix: str | None = None,
    context_file_paths: list[str] | None = None,
) -> CompiledSubAgent:
    """构建基于 DeepAgent 的 outline 子代理（内含 evolution 评估循环）。

    子代理自主决策：生成 → 调用 evolution 评估 → 根据反馈修订（最多 3 轮）。

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:   中间件工厂函数
        style_suffix:        大纲风格 SUFFIX 文本（可选）
        context_file_paths:  上下文文件路径列表（相对于工作区根目录）

    Returns:
        编译后的子代理字典 {name, description, runnable}，可直接注册到 meta_agent
    """
    # ---- 主代理 system prompt + middleware ----
    outline_middleware = list(middleware_factory("outline-subagent"))
    outline_middleware.append(ContextAssemblerMiddleware(
        workspace_root,
        file_paths=context_file_paths or [],
    ))
    primary_spec = build_outline_subagent(outline_middleware, style_suffix)

    # ---- evolution 子代理规格 ----
    evaluation_spec = build_evaluation_subagent(
        EvaluationType.OUTLINE,
        workspace_root,
        middleware_factory("evaluation-subagent"),
        context_file_paths=["outline.md", "character/*.md"],
    )

    # ---- 构建 evolution SubAgent dict ----
    evolution = SubAgent(
        name="evolution",
        description="评估 outline.md 的框架结构质量，写入 evaluation.md，返回评分和修订建议。",
        system_prompt=evaluation_spec["system_prompt"],
        permissions=evaluation_spec.get("permissions"),
        middleware=evaluation_spec.get("middleware"),
    )

    # ---- 组装 system prompt：追加 evolution 使用指令 ----
    base_prompt = primary_spec["system_prompt"]
    evolution_suffix = _EVOLUTION_INSTRUCTION_TEMPLATE.format(
        artifact="outline.md",
        evaluation_file="evaluation.md",
        max_revisions=3,
    )
    # 替换旧的"评估机制"段为新的 evolution 指令
    if "评估机制：" in base_prompt:
        base_prompt = base_prompt.split("评估机制：")[0].rstrip()
    system_prompt = f"{base_prompt}\n\n{evolution_suffix}"

    # ---- 调用工厂 ----
    return build_deep_subagent(
        name="outline",
        description=(
            "适用：需要生成、修改、扩展或重排故事大纲、剧情结构、冲突、转折或结局时调用。"
            "内置 evolution 评估循环：生成 outline.md 后自动评估质量，"
            "如果评估建议修订，会基于 evaluation.md 反馈修订大纲，最多 3 轮。"
            "委托时不要只给文件路径；请用自然语言说明本次大纲任务的目标、可用上下文、关键约束和期望产物。"
        ),
        model=model,
        system_prompt=system_prompt,
        evolution_spec=evolution,
        subagent_middleware=primary_spec.get("middleware"),
        backend=backend,
        artifact_paths=[workspace_root / "outline.md"],
        max_revisions=3,
    )


# evolution 指令模板：追加到子代理 system prompt 末尾
_EVOLUTION_INSTRUCTION_TEMPLATE = """\
评估机制（evolution 子代理）：
- 你有一个名为 "evolution" 的子代理，用于评估你的创作产物质量。
- 工作流程：完成 {artifact} 写入后，调用 evolution 子代理评估质量。
- evolution 会读取 {artifact} 并写入评估报告到 {evaluation_file}，然后返回评分和修改建议。
- 如果 evolution 返回"建议修改"或"必须修改"，你**必须**读取 {evaluation_file} 中的详细评估报告，根据核心问题和修改建议修订 {artifact}，然后再次调用 evolution 评估修订后的版本。
- 如果 evolution 返回"无需修改"，直接向父代理返回结果。
- 最多调用 evolution {max_revisions} 次（含首次评估），超过后系统会强制终止评估循环。
- 返回父代理时，请在回复中包含：修订轮数、是否有质量风险。格式示例："修订轮数：1/3\\n质量风险：无"（或具体风险描述）。
"""
