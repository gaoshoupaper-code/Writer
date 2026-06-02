"""GoalMiddleware — 写作目标状态管理中间件。

职责：
  1. 注入目标工具（set_goal / record_goal_completion）到代理的工具列表
  2. 在 system prompt 中追加目标使用指引和当前目标状态
  3. 在模型输出后拦截未达标的最终回答（goal guard）
  4. 阻止并行调用多个目标工具

核心机制：
  - 代理必须在回复用户前调用 set_goal 设定目标
  - 代理必须在最终回答前调用 record_goal_completion 记录完成状态
  - 如果模型尝试直接回答用户但目标未完成，中间件会拦截并跳回模型重新生成
  - 连续拦截超过 3 次后，强制输出失败提示并终止

状态字段（通过 GoalState 扩展 agent state）：
  - goal:                        当前目标文本
  - goal_completed:              目标是否已完成
  - goal_acceptance_evidence:    完成证据
  - goal_completed_for_turn:     目标完成时对应的用户轮次编号
  - goal_output_blocked:         输出是否被拦截
  - goal_output_block_count:     连续拦截次数

使用方式：
  在构建代理时加入中间件列表，GoalState 会自动扩展代理的状态 schema。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ModelRequest, ModelResponse, ResponseT, hook_config
from langchain_core.messages import AIMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.runtime import Runtime
from typing_extensions import override

from app.writer.tools import (
    RECORD_GOAL_COMPLETION_TOOL_DESCRIPTION,
    SET_GOAL_TOOL_DESCRIPTION,
    GoalState,
    build_goal_tools,
)


# ------------------------------------------------------------------
# 目标工具使用指引（注入到 system prompt 中）
# ------------------------------------------------------------------

GOAL_SYSTEM_PROMPT = """## Goal tools

You have two goal tools with separate responsibilities.

1. `set_goal`: use only after user input to establish or modify the current task goal.
2. `record_goal_completion`: use only before the final user-facing answer to record completion status and concrete acceptance evidence.

The goal is the stable, detailed, and concise task objective inferred from the user's latest intent. It should cover the intended outcome, key constraints, and acceptance focus while avoiding lengthy implementation details.
Acceptance evidence records whether the final result satisfies the goal and what observed evidence supports that judgment.

Rules:
- After a user input, call `set_goal` if there is no current goal or the user materially changed the goal.
- For each user input, call `set_goal` at most once.
- Do not call `set_goal` because of tool outputs, subagent outputs, or internal reflection.
- Do not use `set_goal` to record evidence or completion status.
- Before any final answer to the user, call `record_goal_completion` with concrete evidence.
- If the result satisfies the goal, set `completed` to true and include supporting evidence.
- If `completed` is false, continue the task instead of producing a final answer.
- You may only produce a final answer to the user after `completed` is true.
- If the existing goal still matches the user's intent and there is no new acceptance evidence, do not call either goal tool.

Current goal:
{current_goal}"""

# 当没有设定目标时的显示文本
_NO_GOAL_TEXT = "No goal has been set yet."
# 当没有记录验收证据时的显示文本
_NO_ACCEPTANCE_TEXT = "No acceptance evidence has been recorded yet."
# 最大连续拦截次数：超过后强制终止并报告失败
_MAX_GOAL_OUTPUT_BLOCKS = 3
# 连续拦截达到上限时的失败提示
_GOAL_FAILURE_TEXT = (
    "Goal validation failed: the model attempted to produce a final answer 3 consecutive times "
    "before marking the goal as complete."
)


def _goal_system_prompt(state: Mapping[str, Any] | None, system_prompt: str = GOAL_SYSTEM_PROMPT) -> str:
    """根据当前状态生成目标系统提示词。

    将当前目标文本和验收状态替换到提示词模板中，
    让模型在每次调用时都能看到最新的目标状态。
    """
    goal = state.get("goal") if state else None
    current_goal = goal.strip() if isinstance(goal, str) and goal.strip() else _NO_GOAL_TEXT
    acceptance = _format_acceptance_state(state)
    return system_prompt.replace("{current_goal}", f"{current_goal}\n\nAcceptance status:\n{acceptance}")


def _format_acceptance_state(state: Mapping[str, Any] | None) -> str:
    """将验收状态格式化为可读文本，供 system prompt 展示。

    展示内容：完成状态、验收证据、输出是否被拦截。
    """
    if not state:
        return _NO_ACCEPTANCE_TEXT

    completed = state.get("goal_completed")
    evidence = state.get("goal_acceptance_evidence")
    if completed is None and not evidence:
        return _NO_ACCEPTANCE_TEXT

    status = "complete" if completed is True else "not complete" if completed is False else "unknown"
    lines = [f"- Completed: {status}"]
    if isinstance(evidence, str) and evidence.strip():
        lines.append(f"- Evidence: {evidence.strip()}")
    if state.get("goal_output_blocked"):
        lines.append("- Output blocked: final answers are not allowed until Completed is complete for the latest user input.")
    return "\n".join(lines)


class GoalMiddleware(AgentMiddleware[GoalState[ResponseT], ContextT, ResponseT]):
    """写作目标状态管理中间件。

    通过三个通道实现目标管理：
    1. tools 通道：注册 set_goal 和 record_goal_completion 工具
    2. system prompt 通道：在每次模型调用前注入目标状态指引
    3. after_model hook：在模型输出后拦截未达标的最终回答
    """

    state_schema = GoalState  # type: ignore[assignment]

    def __init__(
        self,
        *,
        system_prompt: str = GOAL_SYSTEM_PROMPT,
        set_goal_description: str = SET_GOAL_TOOL_DESCRIPTION,
        record_completion_description: str = RECORD_GOAL_COMPLETION_TOOL_DESCRIPTION,
    ) -> None:
        """
        Args:
            system_prompt:             目标工具使用指引模板
            set_goal_description:      set_goal 工具的描述文本
            record_completion_description: record_goal_completion 工具的描述文本
        """
        super().__init__()
        self.system_prompt = system_prompt
        self.set_goal_description = set_goal_description
        self.record_completion_description = record_completion_description
        # 构建目标工具列表，通过 bind_tools 通道注册到代理
        self.tools = build_goal_tools(set_goal_description, record_completion_description)

    # ------------------------------------------------------------------
    # wrap_model_call：在每次模型调用前注入目标状态到 system prompt
    # ------------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        """同步：在 system prompt 前面插入目标指引块。"""
        new_system_message = self._system_message_with_goal(request)
        return handler(request.override(system_message=new_system_message))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        """异步：在 system prompt 前面插入目标指引块。"""
        new_system_message = self._system_message_with_goal(request)
        return await handler(request.override(system_message=new_system_message))

    def _system_message_with_goal(self, request: ModelRequest[ContextT]) -> SystemMessage:
        """构造包含目标状态的 system message。

        将目标指引块放在 system message 最前面（prompt caching 前缀位置），
        后面跟原始 system message 内容。
        """
        goal_state = request.state if isinstance(request.state, Mapping) else None
        goal_prompt = _goal_system_prompt(goal_state, self.system_prompt)
        goal_block = {"type": "text", "text": goal_prompt}
        if request.system_message is not None:
            # 目标块 + 分隔空行 + 原始 system prompt 内容
            new_system_content = [goal_block, {"type": "text", "text": "\n\n"}, *request.system_message.content_blocks]
        else:
            new_system_content = [goal_block]
        return SystemMessage(content=cast("list[str | dict[str, str]]", new_system_content))

    # ------------------------------------------------------------------
    # after_model hook：模型输出后拦截未达标的最终回答
    # ------------------------------------------------------------------

    @hook_config(can_jump_to=["model"])
    @override
    def after_model(self, state: GoalState[ResponseT], runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """同步：模型输出后的目标完成度检查。

        如果模型尝试输出最终回答但目标未完成，拦截并跳回模型。
        """
        return _guard_goal_completion_before_output(state)

    @hook_config(can_jump_to=["model"])
    @override
    async def aafter_model(self, state: GoalState[ResponseT], runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """异步：模型输出后的目标完成度检查。"""
        return _guard_goal_completion_before_output(state)


def _guard_goal_completion_before_output(state: GoalState[ResponseT]) -> dict[str, Any] | None:
    """核心拦截逻辑：阻止模型在目标未完成时输出最终回答。

    检查流程：
    1. 先检查是否有并行调用多个目标工具的情况，如果有则返回错误
    2. 找到最后一条 AI 消息
    3. 如果 AI 消息包含工具调用，说明还在工具循环中，放行
    4. 如果目标已完成且对应当前用户轮次，放行
    5. 否则拦截：移除 AI 消息 + 递增拦截计数 + 跳回模型
    6. 连续拦截超过上限时，报告失败并终止
    """
    # 第一步：拒绝并行调用多个目标工具
    parallel_goal_error = _reject_parallel_goal_calls(state)
    if parallel_goal_error is not None:
        return parallel_goal_error

    messages = state.get("messages")
    if not messages:
        return None

    # 找到最后一条 AI 消息
    last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
    if not last_ai_msg or last_ai_msg.tool_calls:
        # AI 消息不存在（不应该发生）或还在工具调用循环中 → 放行
        return None
    if _goal_completed_for_latest_user_turn(state):
        # 目标已完成且对应当前用户轮次 → 放行
        return None

    # 目标未完成，模型试图输出最终回答 → 拦截
    block_count = int(state.get("goal_output_block_count") or 0) + 1
    blocked_message = RemoveMessage(id=last_ai_msg.id) if last_ai_msg.id else None

    if block_count >= _MAX_GOAL_OUTPUT_BLOCKS:
        # 连续拦截达到上限，强制终止并报告失败
        return {
            "goal_output_blocked": True,
            "goal_output_block_count": block_count,
            "messages": [message for message in [blocked_message, AIMessage(content=_GOAL_FAILURE_TEXT)] if message],
        }

    # 拦截并跳回模型重新生成（jump_to: "model" 触发重新调用）
    return {
        "jump_to": "model",
        "goal_output_blocked": True,
        "goal_output_block_count": block_count,
        "messages": [blocked_message] if blocked_message else [],
    }


def _reject_parallel_goal_calls(state: GoalState[ResponseT]) -> dict[str, Any] | None:
    """检查并拒绝并行调用多个目标工具。

    目标工具（set_goal / record_goal_completion）不应并行调用，
    每次模型调用最多使用一个目标工具。
    如果检测到并行调用，为所有目标工具返回错误消息。
    """
    messages = state.get("messages")
    if not messages:
        return None

    last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
    if not last_ai_msg or not last_ai_msg.tool_calls:
        return None

    # 筛选出目标工具调用
    goal_calls = [tc for tc in last_ai_msg.tool_calls if tc["name"] in {"set_goal", "record_goal_completion"}]
    if len(goal_calls) <= 1:
        return None

    # 为所有并行目标工具调用返回错误
    return {
        "messages": [
            ToolMessage(
                content="Error: Goal tools should never be called multiple times in parallel. Call only one goal tool per model invocation.",
                tool_call_id=tc["id"],
                status="error",
            )
            for tc in goal_calls
        ]
    }


def _goal_completed_for_latest_user_turn(state: GoalState[ResponseT]) -> bool:
    """检查目标是否在最新用户轮次中已完成。

    通过比较 goal_completed_for_turn 和当前用户轮次编号来判断，
    避免上一轮的目标完成状态影响当前轮次。
    """
    if state.get("goal_completed") is not True:
        return False
    completed_for_turn = state.get("goal_completed_for_turn")
    if not isinstance(completed_for_turn, int):
        return False
    return completed_for_turn == _current_user_turn(state.get("messages", []))


def _current_user_turn(messages: list[Any]) -> int:
    """计算当前用户轮次编号（即消息列表中用户消息的总数）。"""
    return sum(1 for message in messages if _is_user_message(message))


def _is_user_message(message: Any) -> bool:
    """判断消息是否为用户消息。

    兼容两种格式：LangChain 消息对象（type="human"）和字典（role="user"）。
    """
    if isinstance(message, Mapping):
        return message.get("role") == "user" or message.get("type") == "human"
    return getattr(message, "type", None) == "human" or getattr(message, "role", None) == "user"


__all__ = ["GOAL_SYSTEM_PROMPT", "GoalMiddleware"]
