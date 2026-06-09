"""Writing 子代理 — 正文章节写作 + evolution 审查循环。

架构概览：
  本模块基于 DeepAgent 构建 writing 子代理，内置 evolution 审查循环：
  1. writing 子代理生成或修订正文章节（chapter/*.md）
  2. writing 自主调用 evolution 子代理审查章节质量（review/*.md）
  3. evolution 返回修订建议时，writing 自动修订同一章节（最多 3 轮）

核心组件：
  - build_writing_subagent(): 构建单独的 writing 子代理规格（权限 + 提示词）
  - build_writing_deep_subagent(): 构建 DeepAgent 子代理（含 evolution 审查循环）
  - ContextAssemblerMiddleware: 在每个新写作轮次开始时注入上下文
"""

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

# 写作子代理的系统提示词文件路径（统一存放在 writer/prompt/ 目录）
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompt" / "writing_system_prompt.md"


def _apply_style_suffix(system_prompt: str, style_suffix: str | None) -> str:
    """将写作风格文本作为 SUFFIX 追加到系统提示词末尾。"""
    if not style_suffix:
        return system_prompt
    return f"{system_prompt}\n\n{style_suffix}"


class _RunnableSubAgentSpec(TypedDict):
    """可运行的子代理规格（内部类型）。"""
    name: str
    system_prompt: str
    permissions: NotRequired[list[FilesystemPermission]]
    middleware: NotRequired[list[AgentMiddleware]]
    response_format: NotRequired[object]


def build_writing_subagent(middleware: list[AgentMiddleware] | None = None, style_suffix: str | None = None) -> _RunnableSubAgentSpec:
    """构建单独的 writing 子代理规格（不含审查管道）。

    权限配置：
    - 读取：允许读取所有文件（/**）
    - 写入：只允许写入 /chapter/** （正文章节）
    - 拒绝：禁止写入其他所有文件

    Args:
        middleware:     额外中间件列表（可选）
        style_suffix:  写作风格 SUFFIX 文本（可选，追加到系统提示词末尾）

    Returns:
        子代理规格字典
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
            paths=["/chapter/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/**"],
            mode="deny",
        ),
    ]

    spec = _RunnableSubAgentSpec(
        name="writing",
        system_prompt=system_prompt,
        permissions=permissions,
    )
    if middleware is not None:
        spec["middleware"] = middleware
    return spec


def build_writing_deep_subagent(
    workspace_root: Path,
    model: BaseChatModel,
    backend: BackendProtocol,
    middleware_factory: MiddlewareFactory,
    style_suffix: str | None = None,
    context_file_paths: list[str] | None = None,
) -> CompiledSubAgent:
    """构建基于 DeepAgent 的 writing 子代理（内含 evolution 审查循环）。

    子代理自主决策：写作 → 调用 evolution 审查 → 根据反馈修订（最多 3 轮）。

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:   中间件工厂函数
        style_suffix:        写作风格 SUFFIX 文本（可选）
        context_file_paths:  上下文文件路径列表（相对于工作区根目录）

    Returns:
        编译后的子代理字典 {name, description, runnable}
    """
    # ---- 主代理 system prompt + middleware ----
    writing_middleware = list(middleware_factory("writing-subagent"))
    writing_middleware.append(ContextAssemblerMiddleware(
        workspace_root,
        file_paths=context_file_paths or [],
        context_label="写作前置上下文",
    ))
    primary_spec = build_writing_subagent(writing_middleware, style_suffix)

    # ---- evolution 子代理规格 ----
    evaluation_spec = build_evaluation_subagent(
        EvaluationType.WRITING,
        workspace_root,
        middleware_factory("writing-evaluation-subagent"),
        context_file_paths=["outline.md", "character/*.md", "detail/*.md", "chapter/*.md"],
    )

    # ---- 构建 evolution SubAgent dict ----
    evolution = SubAgent(
        name="evolution",
        description="审查正文章节质量，写入 review/ 下对应文件，返回审查结论和修订建议。",
        system_prompt=evaluation_spec["system_prompt"],
        permissions=evaluation_spec.get("permissions"),
        middleware=evaluation_spec.get("middleware"),
    )

    # ---- 组装 system prompt ----
    base_prompt = primary_spec["system_prompt"]
    if "评估机制" in base_prompt or "审查机制" in base_prompt:
        # 截断到评估/审查机制段之前
        for marker in ("评估机制", "审查机制"):
            if marker in base_prompt:
                base_prompt = base_prompt.split(marker)[0].rstrip()
                break
    evolution_suffix = (
        "审查机制（evolution 子代理）：\n"
        "- 你有一个名为 \"evolution\" 的子代理，用于审查你的正文章节质量。\n"
        "- 工作流程：完成 chapter/ 下章节写入后，调用 evolution 子代理审查质量。\n"
        "- evolution 会读取章节文件并写入审查报告到 review/ 目录下对应文件，然后返回审查结论和修订建议。\n"
        "- 如果 evolution 返回\"建议修订\"或\"必须修订\"，你**必须**读取 review/ 下的审查报告，"
        "根据其中的核心问题和修改建议修订同一个章节文件，然后再次调用 evolution 审查修订后的版本。\n"
        "- 如果 evolution 返回\"通过\"或\"无需修改\"，直接向父代理返回结果。\n"
        "- 最多调用 evolution 3 次（含首次审查），超过后系统会强制终止评估循环。\n"
        "- 返回父代理时，请在回复中包含：修订轮数、是否有质量风险、是否需要调整大纲或状态。"
        "格式示例：\"修订轮数：1/3\\n质量风险：无\\n大纲调整：否\"。\n"
        "- 只修订同一个 chapter/chapter-XX.md 文件；不要新建章节，不要修改 outline.md、character/、"
        "state_log.md 或 evaluation.md。如果确实需要调整大纲或状态，只在回复中标记，不要自行修改。"
    )
    system_prompt = f"{base_prompt}\n\n{evolution_suffix}"

    # ---- 调用工厂 ----
    return build_deep_subagent(
        name="writing",
        description=(
            "适用：需要生成、追加或修订单个正文章节时调用；不用于大纲、角色或评估。"
            "内置 evolution 审查循环：写作后自动审查质量，如果审查建议修订会自动修订，最多 3 轮。"
            "输入上下文包含 character/（角色设计）、outline.md（大纲剧情）和 detail/（对应细纲）。"
            "委托时请说明章节编号、本章目标、出场人物和关键约束。"
        ),
        model=model,
        system_prompt=system_prompt,
        evolution_spec=evolution,
        subagent_middleware=primary_spec.get("middleware"),
        backend=backend,
        max_revisions=3,
    )
