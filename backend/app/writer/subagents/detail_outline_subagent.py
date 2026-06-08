"""Detail Outline 子代理 — 单次调用细纲生成管道。

架构概览：
  本模块实现了"细纲生成 → 评估 → 修订"管道：
  1. primary agent（detail-outline）：生成或修订单个细纲文件（detail/overview.md 或 detail/chapter-XX.md）
  2. secondary agent（evaluation）：评估细纲质量（detail/evaluation.md）
  3. 修订循环：evaluation 建议修订时，自动让 detail-outline 重新修订（最多 2 轮）

  每次调用只处理一个文件（overview 或单个章节），由主代理控制章节推进节奏。
  管道复用 outline 模块的 _build_compiled_pipeline_subagent() 通用构建器。

核心组件：
  - build_detail_outline_subagent():          构建细纲子代理规格
  - build_detail_outline_pipeline_subagent(): 构建完整管道

  - ContextAssemblerMiddleware: 细纲子代理的上下文组装中间件
    在每个新轮次开始时，从文件系统读取指定文件并注入。

  - _parse_evaluation(): 评估结果解析
    从 evaluation 代理的文本输出中解析决策（通过/需要修订）。

  - _build_revision_instruction(): 修订指令构建
    将 evaluation 结果和修订要求组合为 detail-outline 代理的修订指令。
"""

from __future__ import annotations

from pathlib import Path
from typing import NotRequired, TypedDict

from deepagents import CompiledSubAgent
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.writer.subagents.outline_subagent import (
    MiddlewareFactory,
    SecondaryDecision,
    _agent_from_subagent_spec,
    _build_compiled_pipeline_subagent,
    _messages_text,
    _required_result,
)
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


# ---------------------------------------------------------------------------
# 管道构建器（公共 API）
# ---------------------------------------------------------------------------


def build_detail_outline_pipeline_subagent(
    workspace_root: Path,
    model: BaseChatModel,
    backend: BackendProtocol,
    middleware_factory: MiddlewareFactory,
    style_suffix: str | None = None,
    context_file_paths: list[str] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledSubAgent:
    """构建细纲管道子代理（单次调用模式）。

    每次调用只处理一个文件（overview.md 或 chapter-XX.md），
    主代理控制章节推进节奏：先调用生成 overview，再逐章调用生成各章细纲。

    管道内部流程：
    1. detail-outline 代理生成/修订单个细纲文件
    2. evaluation 代理评估细纲质量
    3. 如果 evaluation 建议修订，自动让 detail-outline 重新修订（最多 2 轮）

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:   中间件工厂函数
        style_suffix:        细纲风格 SUFFIX 文本（可选）
        context_file_paths:  上下文文件路径列表（相对于工作区根目录），
                             由主代理控制；新阶段时读取这些文件并注入上下文

    Returns:
        编译后的管道子代理
    """
    # 主代理控制上下文文件路径：ContextAssemblerMiddleware 根据传入的文件路径列表
    # 读取工作区文件，在新阶段时注入上下文。阶段检测（ToolMessage 判断）也由该中间件处理。
    project_middleware = list(middleware_factory("detail-outline-subagent"))
    project_middleware.append(ContextAssemblerMiddleware(
        workspace_root,
        file_paths=context_file_paths or [],
    ))

    detail_agent = _agent_from_subagent_spec(
        build_detail_outline_subagent(project_middleware, style_suffix),
        model,
        backend,
    )
    # 评估代理的上下文包含 outline、character 和 detail 文件，
    # 使其能直接读取被评估的细纲文件，无需外部显式传入。
    evaluation_agent = _agent_from_subagent_spec(
        build_evaluation_subagent(
            EvaluationType.DETAIL_OUTLINE,
            workspace_root,
            middleware_factory("detail-outline-evaluation-subagent"),
            context_file_paths=["outline.md", "character/*.md", "detail/*.md"],
        ),
        model,
        backend,
    )
    return _build_compiled_pipeline_subagent(
        name="detail-outline",
        description=(
            "适用：outline.md 通过 evaluation 后，需要将大纲拆解为逐章细纲时调用。"
            "每次调用只生成一个细纲文件（overview.md 或 chapter-XX.md），"
            "内部会完成评估与修订循环，未通过则自动修订，最多 2 轮。"
            "主代理控制章节推进节奏：先调用生成 overview，获取总章节数后逐章调用。"
            "委托时请说明本次要生成的文件（overview 或具体章节）和创作目标。"
        ),
        workspace_root=workspace_root,
        primary_agent=detail_agent,
        secondary_agent=evaluation_agent,
        primary_artifact="detail/",
        secondary_artifact="detail/evaluation.md",
        primary_label="detail-outline",
        secondary_label="evaluation",
        secondary_instruction=(
            "detail/ 目录下对应细纲文件已成功生成。"
            "当前输入已直接提供 outline.md、character/ 和 detail/ 内容；"
            "请基于原始任务中的细纲目标、创作要求、结构约束和评估标准，"
            "评估细纲质量并写入 detail/evaluation.md。"
        ),
        primary_context_loader=None,
        secondary_context_loader=None,
        enable_revision_loop=True,
        max_revision_count=2,
        secondary_result_parser=_parse_evaluation,
        revision_instruction_builder=_build_revision_instruction,
        checkpointer=checkpointer,
    )


# ---------------------------------------------------------------------------
# 修订指令构建
# ---------------------------------------------------------------------------


def _build_revision_instruction(state: dict) -> str:
    """构建细纲修订指令（修订循环中使用）。

    包含：
    - 原始细纲任务
    - 上一轮 detail-outline 的摘要
    - 上一轮 evaluation 的结果
    - 本轮修订要求

    约束：
    - 只修订同一个 detail/ 下的文件
    - 不修改 outline.md、character/ 或其他文件
    """
    return (
        "你正在基于 evaluation 评估结果修订当前细纲文件。\n\n"
        "原始任务：\n"
        f"{_messages_text(state['messages'])}\n\n"
        "上一轮 detail-outline 摘要：\n"
        f"{_required_result(state, 'primary_result')}\n\n"
        "上一轮 evaluation 结果：\n"
        f"{_required_result(state, 'secondary_result')}\n\n"
        "请先读取 detail/evaluation.md 获取完整评估报告，"
        "然后根据其中的核心问题和修改建议修订当前文件。"
        "只修订 detail/ 目录下的文件，不要修改 outline.md、character/ 或其他文件。"
    )


# ======================================================================
# 评估结果解析
# ======================================================================


def _parse_evaluation(result: str) -> SecondaryDecision:
    """从评估代理的文本输出中解析决策。

    解析格式：中文冒号分隔的键值对：
      - 总分：85
      - 修改建议：无需修改
      - 是否需要修订：否

    解析逻辑与 outline 的 _parse_evaluation_result 类似，
    但字段名略有不同（"是否需要修订" vs "是否需要主代理再次调用 outline 修订"）。
    """
    fields = _parse_fields(result)
    score = fields.get("总分", "")
    suggestion = fields.get("修改建议", "")
    needs_revision = fields.get("是否需要修订", "")

    if suggestion not in {"无需修改", "建议修改", "必须修改"}:
        return {
            "decision": "accept",
            "revision_instruction": "",
            "quality_risk": "evaluation 回复缺少可解析的修改建议；已接受当前版本。",
        }
    if needs_revision not in {"是", "否"}:
        return {
            "decision": "accept",
            "revision_instruction": "",
            "quality_risk": "evaluation 回复缺少可解析的修订判断；已接受当前版本。",
        }

    if needs_revision == "是":
        return {
            "decision": "revise",
            "revision_instruction": (
                f"evaluation 评估总分：{score}，修改建议：{suggestion}。"
                "请读取 detail/evaluation.md 获取详细评估报告和修订指令，"
                "按其中的修改建议修订当前文件。"
            ),
        }

    if suggestion != "无需修改":
        return {
            "decision": "accept",
            "revision_instruction": "",
            "quality_risk": (
                f'evaluation 结论为"{suggestion}"但未要求修订；已接受当前版本。'
            ),
        }
    return {"decision": "accept", "revision_instruction": ""}


def _parse_fields(text: str) -> dict[str, str]:
    """从评估结果文本中解析中文冒号分隔的键值对。

    只提取预定义的键：总分、修改建议、是否需要修订。
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "：" not in line:
            continue
        key, value = line.split("：", 1)
        key = key.strip().removeprefix("-").strip()
        if key in {"总分", "修改建议", "是否需要修订"}:
            fields[key] = value.strip()
    return fields
