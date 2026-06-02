"""Detail Outline 子代理 — 逐章细纲生成管道。

架构概览：
  本模块实现了多阶段细纲生成管道，按顺序生成：
    overview → evaluate → chapter-01 → evaluate → ... → chapter-N → evaluate

  每个阶段都经过 evaluation 评估，未通过则自动修订（最多 2 轮）。

  管道流程（StateGraph）：
    START → init → primary → validate_primary → secondary → validate_secondary
          → parse_secondary → [revise / advance_with_risk / advance]
          → revision_primary → validate_primary → ...（修订循环）
          → advance_phase → [primary / final] → END

  与 outline/writing 管道的关键差异：
  - 多阶段顺序执行（overview → chapter-01 → ... → chapter-N）
  - 每个阶段有独立的修订循环
  - 章节总数在 overview 生成后从文件中解析（运行时确定）
  - 上下文通过 ContextAssemblerMiddleware 注入（由主代理配置文件路径，合并阶段检测和上下文组装）

核心组件：
  - build_detail_outline_subagent():          构建细纲子代理规格
  - build_detail_outline_pipeline_subagent(): 构建完整管道
  - _build_pipeline():                        构建内部 StateGraph
  - _parse_evaluation():                      解析评估结果
  - _parse_chapter_count():                   从 overview.md 解析章节总数

阶段说明：
  - overview:  生成 detail/overview.md（章节规划总览与线索调度）
  - chapter-XX: 生成 detail/chapter-XX.md（逐章规划）
"""

from __future__ import annotations

from pathlib import Path
from typing import NotRequired, TypedDict

from deepagents import CompiledSubAgent
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langgraph.graph import END, START, StateGraph

from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.writer.subagents.evaluation import EvaluationType, build_evaluation_subagent
from app.writer.subagents.outline.outline_subagent import (
    MiddlewareFactory,
    SecondaryDecision,
    _accumulated_messages,
    _agent_from_subagent_spec,
    _artifact_context,
    _child_config,
    _extract_text,
    _markdown_dir_context,
    _markdown_file_context,
    _messages_text,
    _require_non_empty_artifact,
    _required_result,
)

# 细纲子代理的系统提示词文件路径
DETAIL_OUTLINE_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompt" / "detail_outline_system_prompt.txt"
)


def _append_style(system_prompt: str, style_text: str | None) -> str:
    """将写作风格文本追加到系统提示词末尾。"""
    if not style_text:
        return system_prompt
    return f"{system_prompt}\n\n---\n{style_text}\n---"


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


class _DetailState(TypedDict):
    """细纲管道内部状态。

    Fields:
        messages:           输入消息列表（来自父代理的委托）
        phase:              当前阶段（"overview" / "chapter-01" / ... / "done"）
        total_chapters:     总章节数（从 overview.md 解析）
        primary_result:     主代理（detail-outline）的文本输出
        secondary_result:   评估代理的文本输出
        secondary_messages: 评估代理的消息累积
        revision_count:     当前阶段的修订轮次计数
        max_revision_count: 最大修订轮次
        revision_instruction: 当前修订指令
        quality_risks:      各阶段累积的质量风险列表
    """
    messages: list[AnyMessage]
    phase: NotRequired[str]
    total_chapters: NotRequired[int]
    primary_result: NotRequired[str]
    secondary_result: NotRequired[str]
    secondary_messages: NotRequired[list[AnyMessage]]
    revision_count: NotRequired[int]
    max_revision_count: NotRequired[int]
    revision_instruction: NotRequired[str]
    quality_risks: NotRequired[list[str]]


class _DetailOutput(TypedDict):
    """细纲管道输出，只包含 messages 字段。"""
    messages: list[AnyMessage]


# ---------------------------------------------------------------------------
# 子代理规格构建器
# ---------------------------------------------------------------------------


def build_detail_outline_subagent(
    middleware: list[AgentMiddleware] | None = None,
    style_text: str | None = None,
) -> _SubAgentSpec:
    """构建细纲子代理规格。

    权限配置：
    - 读取：允许读取所有文件（/**）
    - 写入：只允许写入 /detail/**（细纲文件）
    - 拒绝：禁止写入其他所有文件

    Args:
        middleware:  额外中间件列表（可选）
        style_text:  写作风格文本（可选）

    Returns:
        子代理规格字典
    """
    system_prompt = _append_style(DETAIL_OUTLINE_PROMPT_PATH.read_text(encoding="utf-8").strip(), style_text)
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
    style_text: str | None = None,
    context_file_paths: list[str] | None = None,
) -> CompiledSubAgent:
    """构建细纲管道子代理。

    管道按顺序执行：
    1. 生成 detail/overview.md → 评估 → 修订（如需要）
    2. 生成 detail/chapter-01.md → 评估 → 修订（如需要）
    3. ...
    4. 生成 detail/chapter-N.md → 评估 → 修订（如需要）
    5. 输出汇总结果

    章节总数在运行时从 overview.md 的"章节总数"字段解析，
    因为在构建管道时大纲还未生成，章节数未知。

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:   中间件工厂函数
        style_text:          写作风格文本（可选）
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
        build_detail_outline_subagent(project_middleware, style_text),
        model,
        backend,
    )
    evaluation_agent = _agent_from_subagent_spec(
        build_evaluation_subagent(
            EvaluationType.DETAIL_OUTLINE,
            workspace_root,
            middleware_factory("detail-outline-evaluation-subagent"),
            context_file_paths=["outline.md", "character/*.md"],
        ),
        model,
        backend,
    )
    return _build_pipeline(
        workspace_root=workspace_root,
        primary_agent=detail_agent,
        secondary_agent=evaluation_agent,
        max_revision_count=2,
    )


# ---------------------------------------------------------------------------
# 管道 StateGraph
# ---------------------------------------------------------------------------


def _build_pipeline(
    *,
    workspace_root: Path,
    primary_agent: Runnable,
    secondary_agent: Runnable,
    max_revision_count: int,
) -> CompiledSubAgent:
    """构建细纲管道的 StateGraph。

    节点列表：
    - init:              初始化状态（设置 phase="overview"）
    - primary:           执行主代理（生成/修订当前阶段的文件）
    - validate_primary:  校验主代理产物
    - secondary:         执行评估代理
    - validate_secondary: 校验评估产物
    - parse_secondary:   解析评估结果，决定是否修订
    - revision_primary:  执行修订（重新调用主代理）
    - mark_revision_limit: 标记修订次数达上限的风险
    - advance_phase:     推进到下一阶段
    - final:             输出汇总结果

    Args:
        workspace_root:       工作区根目录
        primary_agent:        主代理（detail-outline）
        secondary_agent:      评估代理
        max_revision_count:   最大修订轮次

    Returns:
        编译后的管道子代理
    """
    detail_dir = workspace_root / "detail"
    detail_dir.mkdir(parents=True, exist_ok=True)

    # ---- 节点函数 ----

    def init_node(state: _DetailState) -> dict:
        """初始化管道状态：设置 phase 为 "overview"。"""
        return {
            "phase": "overview",
            "revision_count": 0,
            "max_revision_count": max_revision_count,
            "quality_risks": [],
        }

    def primary_node(state: _DetailState, config: RunnableConfig) -> dict:
        """执行主代理：根据当前阶段生成任务指令，调用代理生成文件。

        ContextAssemblerMiddleware 会在新轮次时自动注入工作区上下文，
        因此这里只需要发送任务指令。
        """
        phase = _required_result(state, "phase")
        task = _task_instruction(phase)
        result = primary_agent.invoke(
            {"messages": [HumanMessage(content=task)]}, _child_config(config)
        )
        return {
            "primary_result": _extract_text(result),
        }

    async def aprimary_node(state: _DetailState, config: RunnableConfig) -> dict:
        """执行主代理（异步版本）。"""
        phase = _required_result(state, "phase")
        task = _task_instruction(phase)
        result = await primary_agent.ainvoke(
            {"messages": [HumanMessage(content=task)]}, _child_config(config)
        )
        return {
            "primary_result": _extract_text(result),
        }

    def validate_primary(state: _DetailState) -> dict:
        """校验主代理产物：当前阶段的文件必须存在且非空。"""
        phase = _required_result(state, "phase")
        _require_non_empty_artifact(detail_dir / f"{phase}.md", phase)
        return {}

    def secondary_node(state: _DetailState, config: RunnableConfig) -> dict:
        """执行评估代理：发送当前阶段文件和评估指令。"""
        input_messages = _get_secondary_input_messages(state, workspace_root)
        result = secondary_agent.invoke(
            {"messages": input_messages}, _child_config(config),
        )
        return {
            "secondary_result": _extract_text(result),
            "secondary_messages": _accumulated_messages(input_messages, result),
        }

    async def asecondary_node(state: _DetailState, config: RunnableConfig) -> dict:
        """执行评估代理（异步版本）。"""
        input_messages = _get_secondary_input_messages(state, workspace_root)
        result = await secondary_agent.ainvoke(
            {"messages": input_messages}, _child_config(config),
        )
        return {
            "secondary_result": _extract_text(result),
            "secondary_messages": _accumulated_messages(input_messages, result),
        }

    def validate_secondary(state: _DetailState) -> dict:
        """校验评估产物：detail/evaluation.md 必须存在且非空。"""
        _require_non_empty_artifact(detail_dir / "evaluation.md", "evaluation")
        return {}

    def parse_secondary(state: _DetailState) -> dict:
        """解析评估结果，决定是否需要修订。"""
        phase = _required_result(state, "phase")
        decision = _parse_evaluation(_required_result(state, "secondary_result"))
        updates: dict = {
            "revision_instruction": (
                decision.get("revision_instruction", "")
                if decision["decision"] == "revise"
                else ""
            ),
        }
        risk = decision.get("quality_risk")
        if risk:
            risks = list(state.get("quality_risks") or [])
            risks.append(f"[{phase}] {risk}")
            updates["quality_risks"] = risks
        return updates

    def revision_primary(state: _DetailState, config: RunnableConfig) -> dict:
        """执行修订：构建修订指令，重新调用主代理。"""
        task = _revision_message(state)
        result = primary_agent.invoke(
            {"messages": [HumanMessage(content=task)]}, _child_config(config)
        )
        return {
            "primary_result": _extract_text(result),
            "revision_count": state.get("revision_count", 0) + 1,
        }

    async def arevision_primary(state: _DetailState, config: RunnableConfig) -> dict:
        """执行修订（异步版本）。"""
        task = _revision_message(state)
        result = await primary_agent.ainvoke(
            {"messages": [HumanMessage(content=task)]}, _child_config(config)
        )
        return {
            "primary_result": _extract_text(result),
            "revision_count": state.get("revision_count", 0) + 1,
        }

    def mark_revision_limit(state: _DetailState) -> dict:
        """标记当前阶段修订次数达到上限的质量风险。"""
        phase = _required_result(state, "phase")
        risks = list(state.get("quality_risks") or [])
        risks.append(
            f"[{phase}] evaluation 未在 "
            f"{state.get('max_revision_count', max_revision_count)} "
            "轮修订内通过；已接受当前最好版本。"
        )
        return {"quality_risks": risks}

    def advance_phase(state: _DetailState) -> dict:
        """推进到下一阶段。

        阶段推进逻辑：
        - overview → chapter-01（从 overview.md 解析章节总数）
        - chapter-XX → chapter-(XX+1)（递增章节编号）
        - 最后一章 → done（标记管道完成）

        每次推进都会重置 revision_count。
        """
        phase = _required_result(state, "phase")
        updates: dict = {"revision_count": 0}
        if phase == "overview":
            # overview 完成后，解析章节总数以确定后续需要生成多少章
            total = _parse_chapter_count(detail_dir)
            updates["phase"] = "chapter-01"
            updates["total_chapters"] = total
        else:
            num = int(phase.split("-")[1])
            total = state.get("total_chapters", 0)
            if num < total:
                updates["phase"] = f"chapter-{num + 1:02d}"
            else:
                updates["phase"] = "done"
        return updates

    def final_node(state: _DetailState) -> dict:
        """最终节点：汇总所有阶段的结果，输出给父代理。

        输出包含：
        - 生成完成状态
        - 各阶段累积的质量风险（如果有）
        - 最近阶段的 detail-outline 和 evaluation 摘要
        """
        risks = state.get("quality_risks") or []
        risk_summary = (
            "\n质量风险：\n" + "\n".join(f"  - {r}" for r in risks)
            if risks
            else ""
        )
        return {
            "messages": [
                AIMessage(
                    content=(
                        f"detail/ 目录下细纲已全部生成。{risk_summary}\n\n"
                        "detail-outline 摘要（最近阶段）：\n"
                        f"{_required_result(state, 'primary_result')}\n\n"
                        "evaluation 摘要（最近阶段）：\n"
                        f"{_required_result(state, 'secondary_result')}"
                    )
                )
            ]
        }

    # ---- 路由函数 ----

    def route_after_secondary(state: _DetailState) -> str:
        """评估结果路由：决定是修订、推进阶段还是标记风险后推进。

        路由逻辑：
        - 无修订指令 → "advance"（推进到下一阶段）
        - 修订次数达到上限 → "advance_with_risk"（标记风险后推进）
        - 还有修订余量 → "revise"（继续修订）
        """
        if not state.get("revision_instruction"):
            return "advance"
        if (
            state.get("revision_count", 0)
            >= state.get("max_revision_count", max_revision_count)
        ):
            return "advance_with_risk"
        return "revise"

    def route_after_advance(state: _DetailState) -> str:
        """阶段推进后路由：所有章节完成 → final，否则 → primary。"""
        return "final" if state.get("phase") == "done" else "primary"

    # ---- 构建状态图 ----

    graph = StateGraph(_DetailState, output_schema=_DetailOutput)
    graph.add_node("init", init_node)
    graph.add_node(
        "primary",
        RunnableLambda(primary_node, afunc=aprimary_node, name="detail_outline_primary"),
    )
    graph.add_node("validate_primary", validate_primary)
    graph.add_node(
        "secondary",
        RunnableLambda(secondary_node, afunc=asecondary_node, name="detail_outline_secondary"),
    )
    graph.add_node("validate_secondary", validate_secondary)
    graph.add_node("parse_secondary", parse_secondary)
    graph.add_node(
        "revision_primary",
        RunnableLambda(revision_primary, afunc=arevision_primary, name="detail_outline_revision"),
    )
    graph.add_node("mark_revision_limit", mark_revision_limit)
    graph.add_node("advance_phase", advance_phase)
    graph.add_node("final", final_node)

    # 基础流程
    graph.add_edge(START, "init")
    graph.add_edge("init", "primary")
    graph.add_edge("primary", "validate_primary")
    graph.add_edge("validate_primary", "secondary")
    graph.add_edge("secondary", "validate_secondary")
    graph.add_edge("validate_secondary", "parse_secondary")

    # 评估结果路由
    graph.add_conditional_edges(
        "parse_secondary",
        route_after_secondary,
        {
            "revise": "revision_primary",         # 继续修订
            "advance_with_risk": "mark_revision_limit",  # 达到上限，标记风险
            "advance": "advance_phase",            # 推进到下一阶段
        },
    )
    # 修订后重新进入校验 → 评估循环
    graph.add_edge("revision_primary", "validate_primary")
    graph.add_edge("mark_revision_limit", "advance_phase")
    # 阶段推进后路由
    graph.add_conditional_edges(
        "advance_phase",
        route_after_advance,
        {"primary": "primary", "final": "final"},
    )
    graph.add_edge("final", END)

    return {
        "name": "detail-outline",
        "description": (
            "适用：outline.md 通过 evaluation 后，需要将大纲拆解为逐章细纲时调用。"
            "内部会依次生成 detail/overview.md 和 detail/chapter-XX.md，"
            "每个阶段都经过 evaluation 评估，未通过则自动修订，最多 2 轮。"
            "委托时请说明大纲的创作目标和可用上下文。"
        ),
        "runnable": graph.compile(),
    }


# ---------------------------------------------------------------------------
# 任务指令构建
# ---------------------------------------------------------------------------


def _task_instruction(phase: str) -> str:
    """构建当前阶段的任务指令。

    ContextAssemblerMiddleware 负责注入工作区上下文，
    此函数只提供任务指令文本，不包含文件内容。

    Args:
        phase: 当前阶段（"overview" 或 "chapter-XX"）

    Returns:
        任务指令文本
    """
    if phase == "overview":
        return (
            "请生成 detail/overview.md（章节规划总览与线索调度）。\n"
            "基于 outline.md 的结构骨架、主线分段、次线和交织点，"
            "规划全书的章节分布、节奏设计和线索调度。"
        )
    return (
        f"请生成 detail/{phase}.md（逐章规划）。\n"
        "基于 outline.md、character/ 目录下的角色文件、detail/overview.md 的规划，"
        "以及之前已完成的章节细纲，生成本章的详细规划。"
    )


def _revision_message(state: _DetailState) -> str:
    """构建修订指令（修订循环中使用）。

    告知代理当前阶段的评估摘要，要求读取 evaluation.md 后修订当前文件。
    """
    phase = _required_result(state, "phase")
    return (
        f"你正在基于 evaluation 评估结果修订当前细纲文件（{phase}）。\n\n"
        f"evaluation 评估摘要：\n{_required_result(state, 'secondary_result')}\n\n"
        "请先读取 detail/evaluation.md 获取完整评估报告，"
        "然后根据其中的核心问题和修改建议修订当前文件。"
    )


# ---------------------------------------------------------------------------
# 评估代理输入构建
# ---------------------------------------------------------------------------


def _secondary_message(state: _DetailState, workspace_root: Path) -> HumanMessage:
    """构建评估代理的首次输入消息。

    基础上下文（outline.md, character/）由 ContextAssemblerMiddleware 自动注入，
    这里只提供阶段特定文件（detail/{phase}.md, detail/overview.md）。
    """
    phase = _required_result(state, "phase")
    sections = [
        _markdown_file_context(workspace_root / "detail" / f"{phase}.md"),
    ]
    if phase != "overview":
        sections.append(_markdown_file_context(workspace_root / "detail" / "overview.md"))
    context = _artifact_context("阶段特定上下文", sections)
    return HumanMessage(
        content=(
            f"原始任务：\n{_messages_text(state['messages'])}\n\n"
            f"当前阶段：{phase}\n\n"
            f"上游子代理返回摘要：\n{_required_result(state, 'primary_result')}\n\n"
            f"{context}\n\n"
            "请完成评估并写入 detail/evaluation.md。"
        )
    )


def _get_secondary_input_messages(state: _DetailState, workspace_root: Path) -> list[AnyMessage]:
    """构建评估代理的输入消息。

    基础上下文（outline.md, character/）由 ContextAssemblerMiddleware 自动注入，
    这里只提供阶段特定文件。
    """
    previous = state.get("secondary_messages")
    if not previous:
        return [_secondary_message(state, workspace_root)]
    phase = _required_result(state, "phase")
    sections = [
        _markdown_file_context(workspace_root / "detail" / f"{phase}.md"),
    ]
    context = _artifact_context("阶段特定上下文", sections)
    return list(previous) + [
        HumanMessage(
            content=(
                f"继续评估。当前阶段：{phase}\n\n"
                f"上游子代理返回摘要：\n{_required_result(state, 'primary_result')}\n\n"
                f"{context}\n\n"
                "请完成评估并写入 detail/evaluation.md。"
            )
        )
    ]


# ---------------------------------------------------------------------------
# 评估结果解析
# ---------------------------------------------------------------------------


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


def _parse_chapter_count(detail_dir: Path) -> int:
    """从已生成的 overview.md 中解析章节总数。

    在 overview 通过评估后调用，这是章节总数唯一可靠可知的时刻。
    管道在构建时大纲还不存在，章节数只能在运行时解析。

    Args:
        detail_dir: detail 目录路径

    Returns:
        章节总数（整数）

    Raises:
        ValueError: 无法从 overview.md 中解析章节总数
    """
    import re

    overview_path = detail_dir / "overview.md"
    content = overview_path.read_text(encoding="utf-8")
    match = re.search(r"章节总数[：:]\s*(\d+)", content)
    if match:
        return int(match.group(1))
    raise ValueError("Cannot parse chapter count from detail/overview.md")
