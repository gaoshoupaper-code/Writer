"""Outline 子代理 — 大纲生成 + 评估管道。

架构概览：
  本模块实现了"主代理 → 子代理"管道模式（pipeline pattern）：
  1. primary agent（outline）：生成或修订 outline.md
  2. secondary agent（evaluation）：评估 outline.md 质量
  3. 可选修订循环：evaluation 建议修订时，自动让 outline 重新修订

  管道流程（StateGraph）：
    START → primary → validate_primary → secondary → validate_secondary
                                                ↓
                                          parse_secondary_result
                                          ↙        ↓        ↘
                                    revise    finish_with_risk  finish
                                      ↓                              ↓
                                revision_primary → validate_primary   final → END
                                      （重新进入评估循环）

核心类型：
  - _PipelineState:    管道状态（消息、结果、修订计数等）
  - _PipelineOutput:   管道输出（只包含 messages）
  - SecondaryDecision: 评估结果解析后的决策（accept / revise）

导出的公共 API：
  - build_outline_subagent():          构建单独的 outline 子代理规格
  - build_outline_pipeline_subagent(): 构建带评估循环的 outline 管道子代理

导出的共享工具函数（供 writing/detail_outline 模块复用）：
  - _agent_from_subagent_spec():       从规格构建可运行的 LangChain 代理
  - _build_compiled_pipeline_subagent(): 构建通用管道 StateGraph
  - _artifact_context():               组装产物上下文文本
  - _markdown_file_context():          读取单个 .md 文件内容
  - _markdown_dir_context():           读取目录下所有 .md 文件内容
  - _messages_text():                  从消息列表提取纯文本
  - _require_non_empty_artifact():     校验产物文件非空
  - _required_result():               从状态中提取必需的结果文本
  - _child_config():                  构建子代理运行配置
  - _accumulated_messages():          累积输入和输出消息
  - _extract_text():                  从代理输出中提取文本
  - _messages():                      从状态中获取输入消息
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from deepagents import CompiledSubAgent
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    compute_summarization_defaults,
)
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.writer.subagents.evaluation_subagent import EvaluationType, build_evaluation_subagent

# 大纲子代理的系统提示词文件路径（统一存放在 writer/prompt/ 目录）
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompt" / "outline_system_prompt.md"


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


def _evaluation_decision_to_secondary(decision: dict) -> SecondaryDecision:
    """将结构化 evaluation_decision（来自 submit_evaluation 工具）转为 SecondaryDecision。

    当 middleware 提供了结构化评估数据时直接使用，无需文本解析。

    处理逻辑：
    - suggestion 不在预定义值中 → 接受但记录质量风险
    - needs_revision 为 True 且有修订指令 → 返回 "revise" 决策
    - needs_revision 为 True 但无修订指令 → 接受但记录质量风险
    - needs_revision 为 False → 接受（可能附带质量风险）
    """
    suggestion = decision.get("suggestion", "")
    needs_revision = decision.get("needs_revision", False)
    score = decision.get("score", "")
    revision_instruction = decision.get("revision_instruction", "")

    if suggestion not in {"无需修改", "建议修改", "必须修改"}:
        return {
            "decision": "accept",
            "revision_instruction": "",
            "quality_risk": "evaluation 结构化决策缺少有效的修改建议；已接受当前版本。",
        }

    if needs_revision:
        if not revision_instruction.strip():
            return {
                "decision": "accept",
                "revision_instruction": "",
                "quality_risk": "evaluation 要求修订但未提供修订指令；已接受当前版本。",
            }
        return {
            "decision": "revise",
            "revision_instruction": (
                f"evaluation 评估总分：{score}，修改建议：{suggestion}。"
                "请读取 evaluation.md 获取详细评估报告，按其中的修改建议修订 outline.md。"
            ),
        }

    if suggestion != "无需修改":
        return {
            "decision": "accept",
            "revision_instruction": "",
            "quality_risk": f"evaluation 结论为\"{suggestion}\"但未要求修订；已接受当前版本。",
        }
    return {"decision": "accept", "revision_instruction": ""}


# ======================================================================
# 类型定义
# ======================================================================

# 中间件工厂函数类型：根据代理名称生成中间件列表
MiddlewareFactory = Callable[[str], list[AgentMiddleware]]
# 上下文加载器类型：返回工作区上下文字符串
ContextLoader = Callable[[], str]
# 评估决策值类型
SecondaryDecisionValue = Literal["accept", "revise"]


class SecondaryDecision(TypedDict):
    """评估结果解析后的决策。

    Fields:
        decision:            "accept"（接受当前版本）或 "revise"（需要修订）
        revision_instruction: 修订指令（仅 decision="revise" 时有值）
        quality_risk:        质量风险提示（可选，记录不确定的情况）
    """
    decision: SecondaryDecisionValue
    revision_instruction: str
    quality_risk: NotRequired[str]


# 评估结果解析函数类型
SecondaryResultParser = Callable[[str], SecondaryDecision]
# 修订指令构建函数类型
RevisionInstructionBuilder = Callable[["_PipelineState"], str]


def _parse_evaluation_result(result: str) -> SecondaryDecision:
    """从评估代理的文本输出中解析决策（文本解析的回退方案）。

    解析格式：中文冒号分隔的键值对，如：
      - 总分：85
      - 修改建议：无需修改
      - 是否需要主代理再次调用 outline 修订：否

    解析失败时默认接受当前版本，并在 quality_risk 中记录原因。
    """
    fields = _parse_evaluation_fields(result)
    score = fields.get("总分")
    suggestion = fields.get("修改建议")
    needs_revision = fields.get("是否需要主代理再次调用 outline 修订")

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
                "请读取 evaluation.md 获取详细评估报告和修订指令，按其中的修改建议修订 outline.md。"
            ),
        }

    if suggestion != "无需修改":
        return {
            "decision": "accept",
            "revision_instruction": "",
            "quality_risk": f"evaluation 结论为\"{suggestion}\"但未要求 outline 修订；已接受当前版本。",
        }
    return {"decision": "accept", "revision_instruction": ""}


def _parse_evaluation_fields(text: str) -> dict[str, str]:
    """从文本中解析中文冒号分隔的键值对。

    只提取预定义的键：总分、修改建议、是否需要主代理再次调用 outline 修订、evaluation.md
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "：" not in line:
            continue
        key, value = line.split("：", 1)
        key = key.strip().removeprefix("-").strip()
        if key in {
            "总分",
            "修改建议",
            "是否需要主代理再次调用 outline 修订",
            "evaluation.md",
        }:
            fields[key] = value.strip()
    return fields


def _build_outline_revision_instruction(state: "_PipelineState") -> str:
    """构建大纲修订指令（修订循环中使用）。

    告知 outline 代理当前的评估摘要，要求读取 evaluation.md 后修订 outline.md。
    """
    return (
        "你正在基于 evaluation 评估结果修订 outline.md。\n\n"
        "evaluation 评估摘要：\n"
        f"{_required_result(state, 'secondary_result')}\n\n"
        "请先读取 evaluation.md 获取完整评估报告，然后根据其中的核心问题和修改建议修订 outline.md。"
    )


# ======================================================================
# 管道状态类型
# ======================================================================

class _PipelineState(TypedDict):
    """管道内部状态，在 StateGraph 的各个节点间传递。

    Fields:
        messages:           输入消息列表（来自父代理的委托）
        primary_result:     主代理（outline）的文本输出
        secondary_result:   次代理（evaluation）的文本输出
        primary_messages:   主代理的消息累积（用于修订时延续上下文）
        revision_count:     当前修订轮次计数
        max_revision_count: 最大修订轮次
        revision_instruction: 当前修订指令
        quality_risk:       质量风险提示
        evaluation_decision: 结构化评估决策（来自 submit_evaluation 工具）
    """
    messages: list[AnyMessage]
    primary_result: NotRequired[str]
    secondary_result: NotRequired[str]
    primary_messages: NotRequired[list[AnyMessage]]
    revision_count: NotRequired[int]
    max_revision_count: NotRequired[int]
    revision_instruction: NotRequired[str]
    quality_risk: NotRequired[str]
    evaluation_decision: NotRequired[dict | None]


class _PipelineOutput(TypedDict):
    """管道输出，只包含 messages 字段（最终回复给父代理）。"""
    messages: list[AnyMessage]


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


# ======================================================================
# 子代理构建函数
# ======================================================================

def build_outline_subagent(middleware: list[AgentMiddleware] | None = None, style_suffix: str | None = None) -> _RunnableSubAgentSpec:
    """构建单独的 outline 子代理规格（不含评估管道）。

    权限配置：
    - 读取：允许读取所有文件（/**）
    - 写入：只允许写入 /outline.md
    - 拒绝：禁止写入其他所有文件

    Args:
        middleware:     额外中间件列表（可选）
        style_suffix:  大纲风格 SUFFIX 文本（可选，追加到系统提示词末尾）

    Returns:
        子代理规格字典，供 _agent_from_subagent_spec 使用
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


def build_outline_pipeline_subagent(
    workspace_root: Path,
    model: BaseChatModel,
    backend: BackendProtocol,
    middleware_factory: MiddlewareFactory,
    style_suffix: str | None = None,
    context_file_paths: list[str] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledSubAgent:
    """构建带评估循环的 outline 管道子代理。

    管道流程：
    1. outline 代理生成/修订 outline.md
    2. evaluation 代理评估 outline.md 并写入 evaluation.md
    3. 如果 evaluation 建议修订，自动让 outline 重新修订（最多 2 轮）
    4. 输出汇总结果给父代理

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:   中间件工厂函数
        style_suffix:        大纲风格 SUFFIX 文本（可选）
        context_file_paths:  上下文文件路径列表（相对于工作区根目录），
                             由主代理控制；新阶段时读取这些文件并注入上下文

    Returns:
        编译后的管道子代理，可直接委托给主代理
    """
    # 主代理控制上下文文件路径：ContextAssemblerMiddleware 根据传入的文件路径列表
    # 读取工作区文件，在新阶段时注入上下文。阶段检测（ToolMessage 判断）也由该中间件处理。
    outline_middleware = list(middleware_factory("outline-subagent"))
    outline_middleware.append(ContextAssemblerMiddleware(
        workspace_root,
        file_paths=context_file_paths or [],
    ))
    outline_agent = _agent_from_subagent_spec(
        build_outline_subagent(outline_middleware, style_suffix),
        model,
        backend,
    )
    evaluation_agent = _agent_from_subagent_spec(
        build_evaluation_subagent(
            EvaluationType.OUTLINE,
            workspace_root,
            middleware_factory("evaluation-subagent"),
            context_file_paths=["outline.md", "character/*.md"],
        ),
        model,
        backend,
    )
    return _build_compiled_pipeline_subagent(
        name="outline",
        description=(
            "适用：需要生成、修改、扩展或重排故事大纲、剧情结构、冲突、转折或结局时调用。"
            "内部会先完成 outline.md 写入或更新，然后在成功后调用 evaluation 写入 evaluation.md；"
            "如果 evaluation 建议修订，会自动让 outline 基于 evaluation.md 的反馈修订大纲，最多 2 轮。"
            "委托时不要只给文件路径；请用自然语言说明本次大纲任务的目标、可用上下文、关键约束和期望产物。"
        ),
        workspace_root=workspace_root,
        primary_agent=outline_agent,
        secondary_agent=evaluation_agent,
        primary_artifact="outline.md",
        secondary_artifact="evaluation.md",
        primary_label="outline",
        secondary_label="evaluation",
        secondary_instruction=(
            "outline.md 已成功写入或更新。当前输入已直接提供 outline.md 内容；"
            "请基于原始任务中的创作目标、可用上下文、关键约束和期望产物，"
            "并按需参考 character/、novel.md、state_log.md、review/ 章节审查文件，完成评估并写入 evaluation.md。"
        ),
        enable_revision_loop=True,
        max_revision_count=2,
        secondary_result_parser=_parse_evaluation_result,
        revision_instruction_builder=_build_outline_revision_instruction,
        checkpointer=checkpointer,
    )


# ======================================================================
# 通用代理构建器
# ======================================================================


def _build_summarization_middleware(
    model: BaseChatModel,
    backend: BackendProtocol,
) -> SummarizationMiddleware:
    """构建 SummarizationMiddleware，固定消息数策略。

    触发阈值由模型 profile 决定（有 profile 用 85% 分数，无 profile 用 170K tokens）。
    保留策略固定为消息数：
    - 摘要保留：最后 10 条消息
    - 参数截断触发：>25 条消息
    - 参数截断保留：最后 25 条消息
    """
    defaults = compute_summarization_defaults(model)
    return SummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=defaults["trigger"],
        keep=("messages", 10),
        truncate_args_settings={
            "trigger": ("messages", 25),
            "keep": ("messages", 25),
            "max_length": 3000,
        },
    )


def _agent_from_subagent_spec(
    spec: _RunnableSubAgentSpec,
    model: BaseChatModel,
    backend: BackendProtocol,
) -> Runnable:
    """从子代理规格构建可运行的 LangChain 代理。

    中间件组装顺序（从内到外）：
    1. TodoListMiddleware          — 待办事项管理
    2. FilesystemMiddleware        — 文件系统操作（含权限控制）
    3. SummarizationMiddleware     — 长对话自动摘要
    4. PatchToolCallsMiddleware    — 工具调用修补
    5. 项目中间件                  — 路径守卫、追踪等
    6. AnthropicPromptCachingMiddleware — Anthropic 提示缓存

    Args:
        spec:   子代理规格
        model:  聊天模型
        backend: DeepAgents 后端

    Returns:
        可运行的 LangChain 代理
    """
    middleware: list[AgentMiddleware] = [
        TodoListMiddleware(),
        FilesystemMiddleware(backend=backend, _permissions=spec.get("permissions")),
        _build_summarization_middleware(model, backend),
        PatchToolCallsMiddleware(),
    ]
    # 追加项目自定义中间件
    middleware.extend(spec.get("middleware", []))
    # Anthropic 缓存放在最后，确保它看到的是干净的消息格式
    middleware.append(AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"))
    return create_agent(
        model,
        system_prompt=spec["system_prompt"],
        tools=[],
        middleware=middleware,
        name=spec["name"],
        response_format=spec.get("response_format"),
    )


# ======================================================================
# 通用管道构建器（供 outline / writing / detail_outline 复用）
# ======================================================================

def _build_compiled_pipeline_subagent(
    *,
    name: str,
    description: str,
    workspace_root: Path,
    primary_agent: Runnable,
    secondary_agent: Runnable,
    primary_artifact: str,
    secondary_artifact: str,
    primary_label: str,
    secondary_label: str,
    secondary_instruction: str,
    primary_context_loader: ContextLoader | None = None,
    secondary_context_loader: ContextLoader | None = None,
    enable_revision_loop: bool = False,
    max_revision_count: int = 0,
    secondary_result_parser: SecondaryResultParser | None = None,
    revision_instruction_builder: RevisionInstructionBuilder | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledSubAgent:
    """构建通用的"主代理 + 评估代理"管道 StateGraph。

    管道流程（启用修订循环时）：
      START → primary → validate_primary → secondary → validate_secondary
          → parse_secondary_result → [revise / finish_with_risk / finish]
          → revision_primary → validate_primary → secondary → ...
          → final → END

    管道流程（不启用修订循环时）：
      START → primary → validate_primary → secondary → validate_secondary → final → END

    Args:
        name:                       管道名称
        description:                管道描述（供主代理选择委托目标）
        workspace_root:             工作区根目录
        primary_agent:              主代理（如 outline / writing）
        secondary_agent:            评估代理（如 evaluation / review）
        primary_artifact:           主代理产物文件名（如 "outline.md"）
        secondary_artifact:         评估代理产物文件名（如 "evaluation.md"）
        primary_label:              主代理标签（用于日志和输出）
        secondary_label:            评估代理标签
        secondary_instruction:      评估代理的任务指令
        primary_context_loader:     主代理的上下文加载器（可选）
        secondary_context_loader:   评估代理的上下文加载器（可选）
        enable_revision_loop:       是否启用修订循环
        max_revision_count:         最大修订轮次（启用循环时必须 >= 1）
        secondary_result_parser:    评估结果解析函数（启用循环时必须提供）
        revision_instruction_builder: 修订指令构建函数（启用循环时必须提供）

    Returns:
        编译后的管道子代理字典 {name, description, runnable}
    """
    if enable_revision_loop:
        if max_revision_count < 1:
            raise ValueError("Revision loop requires max_revision_count >= 1.")
        if secondary_result_parser is None:
            raise ValueError("Revision loop requires secondary_result_parser.")
        if revision_instruction_builder is None:
            raise ValueError("Revision loop requires revision_instruction_builder.")

    # 产物文件路径（用于 validate 节点校验）
    primary_path = workspace_root / primary_artifact
    secondary_path = workspace_root / secondary_artifact

    # ---- 主代理节点 ----

    def primary_node(state: _PipelineState, config: RunnableConfig) -> dict[str, str | list[AnyMessage]]:
        """执行主代理：发送输入消息，收集输出。"""
        input_messages = _messages_with_context(state, primary_context_loader)
        result = primary_agent.invoke(_agent_input(input_messages), _child_config(config))
        return {
            "primary_result": _extract_text(result),
            "primary_messages": _accumulated_messages(input_messages, result),
        }

    async def aprimary_node(state: _PipelineState, config: RunnableConfig) -> dict[str, str | list[AnyMessage]]:
        """执行主代理（异步版本）。"""
        input_messages = _messages_with_context(state, primary_context_loader)
        result = await primary_agent.ainvoke(_agent_input(input_messages), _child_config(config))
        return {
            "primary_result": _extract_text(result),
            "primary_messages": _accumulated_messages(input_messages, result),
        }

    # ---- 修订节点（修订循环时使用） ----

    def revision_primary_node(state: _PipelineState, config: RunnableConfig) -> dict[str, str | int | list[AnyMessage]]:
        """执行修订：延续主代理的上下文，追加修订指令，重新调用主代理。"""
        if revision_instruction_builder is None:
            raise ValueError("Revision instruction builder is required for revision node.")
        carry_over = _carry_over_messages(state)
        carry_over.append(HumanMessage(content=revision_instruction_builder(state)))
        result = primary_agent.invoke(_agent_input(carry_over), _child_config(config))
        return {
            "primary_result": _extract_text(result),
            "primary_messages": _accumulated_messages(carry_over, result),
            "revision_count": state.get("revision_count", 0) + 1,
        }

    async def arevision_primary_node(state: _PipelineState, config: RunnableConfig) -> dict[str, str | int | list[AnyMessage]]:
        """执行修订（异步版本）。"""
        if revision_instruction_builder is None:
            raise ValueError("Revision instruction builder is required for revision node.")
        carry_over = _carry_over_messages(state)
        carry_over.append(HumanMessage(content=revision_instruction_builder(state)))
        result = await primary_agent.ainvoke(_agent_input(carry_over), _child_config(config))
        return {
            "primary_result": _extract_text(result),
            "primary_messages": _accumulated_messages(carry_over, result),
            "revision_count": state.get("revision_count", 0) + 1,
        }

    # ---- 校验节点 ----

    def validate_primary_node(_state: _PipelineState) -> dict[str, str]:
        """校验主代理产物：文件必须存在且非空。"""
        del _state
        _require_non_empty_artifact(primary_path, primary_label)
        return {}

    def validate_secondary_node(_state: _PipelineState) -> dict[str, str]:
        """校验评估代理产物：文件必须存在且非空。"""
        del _state
        _require_non_empty_artifact(secondary_path, secondary_label)
        return {}

    # ---- 评估节点 ----

    def secondary_node(state: _PipelineState, config: RunnableConfig) -> dict[str, str | list[AnyMessage] | dict | None]:
        """执行评估代理：发送主代理的输出和评估指令，收集评估结果。"""
        input_messages = _get_secondary_input_messages(state, secondary_instruction, secondary_context_loader)
        result = secondary_agent.invoke(_agent_input(input_messages), _child_config(config))
        updates: dict[str, str | list[AnyMessage] | dict | None] = {
            "secondary_result": _extract_text(result),
            "evaluation_decision": None,
        }
        # 从 agent 输出中提取结构化评估决策（如果 middleware 提供了的话）
        if isinstance(result, Mapping):
            eval_decision = result.get("evaluation_decision")
            if isinstance(eval_decision, dict):
                updates["evaluation_decision"] = eval_decision
        return updates

    async def asecondary_node(state: _PipelineState, config: RunnableConfig) -> dict[str, str | list[AnyMessage] | dict | None]:
        """执行评估代理（异步版本）。"""
        input_messages = _get_secondary_input_messages(state, secondary_instruction, secondary_context_loader)
        result = await secondary_agent.ainvoke(_agent_input(input_messages), _child_config(config))
        updates: dict[str, str | list[AnyMessage] | dict | None] = {
            "secondary_result": _extract_text(result),
            "evaluation_decision": None,
        }
        if isinstance(result, Mapping):
            eval_decision = result.get("evaluation_decision")
            if isinstance(eval_decision, dict):
                updates["evaluation_decision"] = eval_decision
        return updates

    # ---- 解析节点 ----

    def parse_secondary_result_node(state: _PipelineState) -> dict[str, str]:
        """解析评估结果，决定是否需要修订。

        优先使用结构化评估决策（来自 submit_evaluation 工具），
        如果没有则回退到文本解析（secondary_result_parser）。
        """
        if secondary_result_parser is None:
            raise ValueError("Secondary result parser is required for revision loop.")
        # 优先使用结构化评估决策（来自 submit_evaluation 工具）
        eval_decision = state.get("evaluation_decision")
        if isinstance(eval_decision, dict) and "suggestion" in eval_decision:
            decision = _evaluation_decision_to_secondary(eval_decision)
        else:
            # 回退到文本解析
            decision = secondary_result_parser(_required_result(state, "secondary_result"))
        updates: dict[str, str] = {
            "revision_instruction": decision["revision_instruction"] if decision["decision"] == "revise" else ""
        }
        quality_risk = decision.get("quality_risk")
        if quality_risk:
            updates["quality_risk"] = quality_risk
        return updates

    # ---- 路由函数 ----

    def route_after_secondary(state: _PipelineState) -> str:
        """评估结果路由：决定是修订、接受还是达到修订上限。

        路由逻辑：
        - 无修订指令 → "finish"（直接完成）
        - 有质量风险 → "finish"（接受但记录风险）
        - 修订次数达到上限 → "finish_with_risk"（标记风险后完成）
        - 还有修订余量 → "revise"（继续修订）
        """
        if not state.get("revision_instruction"):
            return "finish"
        if state.get("quality_risk"):
            return "finish"
        if state.get("revision_count", 0) >= state.get("max_revision_count", max_revision_count):
            return "finish_with_risk"
        return "revise"

    # ---- 特殊节点 ----

    def mark_revision_limit_node(state: _PipelineState) -> dict[str, str]:
        """标记修订次数达到上限的质量风险。"""
        return {
            "quality_risk": (
                f"{secondary_label} 未在 {state.get('max_revision_count', max_revision_count)} 轮修订内通过；"
                "已接受当前最好版本，需主 Agent 在后续规划中留意该章节风险。"
            )
        }

    def final_node(state: _PipelineState) -> dict[str, list[AIMessage]]:
        """最终节点：汇总主代理和评估代理的结果，输出给父代理。

        输出包含：
        - 产物状态（已写入/已更新）
        - 修订轮数（如果启用修订循环）
        - 质量风险提示（如果有）
        - 主代理和评估代理的文本摘要
        """
        quality_risk = state.get("quality_risk")
        revision_summary = ""
        if enable_revision_loop:
            revision_summary = f"\n修订轮数：{state.get('revision_count', 0)}/{state.get('max_revision_count', max_revision_count)}"
        risk_summary = f"\n质量风险：{quality_risk}" if quality_risk else ""
        return {
            "messages": [
                AIMessage(
                    content=(
                        f"{primary_artifact}：已写入或更新\n"
                        f"{secondary_artifact}：已写入或更新"
                        f"{revision_summary}"
                        f"{risk_summary}\n\n"
                        f"{primary_label} 摘要：\n{_required_result(state, 'primary_result')}\n\n"
                        f"{secondary_label} 摘要：\n{_required_result(state, 'secondary_result')}"
                    )
                )
            ]
        }

    # ---- 构建状态图 ----

    graph = StateGraph(_PipelineState, output_schema=_PipelineOutput)
    graph.add_node("primary", RunnableLambda(primary_node, afunc=aprimary_node, name=f"{name}_primary"))
    graph.add_node("validate_primary", validate_primary_node)
    graph.add_node("secondary", RunnableLambda(secondary_node, afunc=asecondary_node, name=f"{name}_secondary"))
    graph.add_node("validate_secondary", validate_secondary_node)
    graph.add_node("final", final_node)

    # 基础流程：START → primary → validate → secondary → validate → ...
    graph.add_edge(START, "primary")
    graph.add_edge("primary", "validate_primary")
    graph.add_edge("validate_primary", "secondary")
    graph.add_edge("secondary", "validate_secondary")

    if enable_revision_loop:
        # 修订循环流程
        graph.add_node(
            "revision_primary",
            RunnableLambda(revision_primary_node, afunc=arevision_primary_node, name=f"{name}_revision_primary"),
        )
        graph.add_node("parse_secondary_result", parse_secondary_result_node)
        graph.add_node("mark_revision_limit", mark_revision_limit_node)
        graph.add_edge("validate_secondary", "parse_secondary_result")
        graph.add_conditional_edges(
            "parse_secondary_result",
            route_after_secondary,
            {
                "revise": "revision_primary",        # 继续修订
                "finish_with_risk": "mark_revision_limit",  # 达到上限，标记风险
                "finish": "final",                    # 直接完成
            },
        )
        # 修订后重新进入校验 → 评估循环
        graph.add_edge("revision_primary", "validate_primary")
        graph.add_edge("mark_revision_limit", "final")
    else:
        # 无修订循环：评估后直接到最终节点
        graph.add_edge("validate_secondary", "final")
    graph.add_edge("final", END)

    return {"name": name, "description": description, "runnable": graph.compile(checkpointer=checkpointer)}


# ======================================================================
# 共享工具函数（供 writing / detail_outline 等模块复用）
# ======================================================================

def _agent_input(messages: list[AnyMessage]) -> dict[str, list[AnyMessage]]:
    """将消息列表包装为代理输入格式。"""
    return {"messages": messages}


def _accumulated_messages(input_messages: list[AnyMessage], result: object) -> list[AnyMessage]:
    """累积输入消息和代理输出消息。

    用于修订循环中延续上下文：将上一轮的输入和输出合并为下一轮的输入。
    """
    accumulated = list(input_messages)
    if isinstance(result, Mapping):
        result_messages = result.get("messages", [])
        if isinstance(result_messages, list):
            accumulated.extend(result_messages)
    return accumulated


def _carry_over_messages(state: _PipelineState) -> list[AnyMessage]:
    """提取上一轮主代理的消息作为修订轮次的延续上下文。

    优先使用 primary_messages（主代理的完整对话历史），
    如果没有则回退到原始输入消息。
    """
    primary_messages = state.get("primary_messages")
    if primary_messages:
        return list(primary_messages)
    return list(_messages(state))


def _messages(state: _PipelineState) -> list[AnyMessage]:
    """从状态中获取输入消息列表。"""
    messages = state.get("messages")
    if not messages:
        raise ValueError("Pipeline subagent requires at least one input message.")
    return messages


def _messages_with_context(state: _PipelineState, context_loader: ContextLoader | None) -> list[AnyMessage]:
    """将上下文加载器的内容追加到消息列表末尾。

    如果 context_loader 返回非空内容，将其作为 HumanMessage 追加。
    """
    messages = list(_messages(state))
    context = _load_context(context_loader)
    if context:
        messages.append(HumanMessage(content=context))
    return messages


def _secondary_messages(
    state: _PipelineState,
    instruction: str,
    context_loader: ContextLoader | None = None,
) -> list[HumanMessage]:
    """构建评估代理的首次输入消息。

    包含：原始任务文本 + 上下文 + 主代理摘要 + 评估指令。
    """
    context = _load_context(context_loader)
    context_section = f"\n\n用户提供的文件上下文：\n{context}" if context else ""
    return [
        HumanMessage(
            content=(
                f"原始任务：\n{_messages_text(_messages(state))}"
                f"{context_section}\n\n"
                f"上游子代理返回摘要：\n{_required_result(state, 'primary_result')}\n\n"
                f"后置任务：\n{instruction}"
            )
        )
    ]


def _get_secondary_input_messages(
    state: _PipelineState,
    instruction: str,
    context_loader: ContextLoader | None = None,
) -> list[AnyMessage]:
    """构建评估代理的输入消息。

    评估代理每次调用都重新构建输入，避免把上轮对话历史带入下一轮，
    从而让 ContextAssemblerMiddleware 始终基于当前文件重建上下文。
    """
    return _secondary_messages(state, instruction, context_loader)


def _load_context(context_loader: ContextLoader | None) -> str:
    """调用上下文加载器，返回加载的文本内容。"""
    if context_loader is None:
        return ""
    return context_loader().strip()


def _child_config(config: RunnableConfig | None) -> RunnableConfig:
    """从父配置中提取子代理需要的配置项（callbacks、tags、configurable）。"""
    if not config:
        return {}
    return cast(
        RunnableConfig,
        {key: config[key] for key in ("callbacks", "tags", "configurable") if key in config},
    )


def _require_non_empty_artifact(path: Path, label: str) -> None:
    """校验产物文件/目录存在且内容非空。

    支持两种模式：
    - 路径是目录：检查目录中是否有非空的 .md 文件
    - 路径是文件：检查文件存在且非空
    """
    if path.is_dir():
        if any(child.is_file() and child.suffix == ".md" and child.read_text(encoding="utf-8").strip() for child in path.iterdir()):
            return
        raise FileNotFoundError(f"{label} subagent did not write a non-empty Markdown file under {path}")
    if not path.exists():
        raise FileNotFoundError(f"{label} subagent did not write {path.name}: {path}")
    if not path.read_text(encoding="utf-8").strip():
        raise ValueError(f"{label} subagent wrote an empty {path.name}: {path}")


def _artifact_context(title: str, sections: list[str]) -> str:
    """将多个上下文段落组合成带标题的上下文文本。"""
    content = "\n\n".join(section for section in sections if section.strip()).strip()
    if not content:
        return ""
    return f"{title}：\n{content}"


def _markdown_file_context(path: Path) -> str:
    """读取单个 Markdown 文件并包装为带标签的上下文块。"""
    if not path.is_file():
        return ""
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    return f"文件：/{path.name}\n```markdown\n{content}\n```"


def _markdown_dir_context(path: Path) -> str:
    """读取目录下所有 Markdown 文件并包装为上下文块。"""
    if not path.is_dir():
        return ""
    sections = []
    for child in sorted(path.iterdir()):
        if child.is_file() and child.suffix == ".md":
            content = child.read_text(encoding="utf-8").strip()
            if content:
                sections.append(f"文件：/{path.name}/{child.name}\n```markdown\n{content}\n```")
    return "\n\n".join(sections)


def _required_result(state: Mapping[str, object], key: str) -> str:
    """从状态中提取必需的结果文本。

    如果键不存在或值为空，抛出 ValueError。
    """
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Pipeline state is missing non-empty {key}.")
    return value.strip()


def _extract_text(result: object) -> str:
    """从代理输出中提取文本内容。

    从消息列表中反向搜索最后一条包含文本内容的消息。
    支持字符串内容和列表内容（多模态消息格式）。
    """
    if isinstance(result, Mapping):
        messages = result.get("messages", [])
        if isinstance(messages, list):
            for message in reversed(messages):
                content = _message_content(message)
                if isinstance(content, str) and content.strip():
                    return content.strip()
                if isinstance(content, list):
                    text = "\n".join(
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    ).strip()
                    if text:
                        return text
    text = str(result).strip()
    if not text:
        raise ValueError("Agent returned an empty result.")
    return text


def _message_content(message: object) -> object:
    """从消息对象中提取内容字段。

    兼容 BaseMessage、字典和普通对象。
    """
    if isinstance(message, BaseMessage):
        return message.content
    if isinstance(message, Mapping):
        return message.get("content")
    return getattr(message, "content", None)


def _messages_text(messages: list[AnyMessage]) -> str:
    """从消息列表中提取所有文本内容，合并为单个字符串。

    用于将消息列表转为纯文本传递给评估代理。
    """
    chunks = []
    for message in messages:
        content = _message_content(message)
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            chunks.extend(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
    text = "\n".join(chunk for chunk in chunks if chunk).strip()
    if not text:
        raise ValueError("Pipeline input messages do not contain text.")
    return text
