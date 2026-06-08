"""GoalMiddleware — 写作目标工具注册 + 输出拦截中间件。

职责：
  1. 注册 set_goal / record_goal_completion 工具和 GoalState 状态 schema
  2. 在模型输出后拦截未达标的最终回答（goal guard）

不修改 system prompt，保持缓存前缀稳定。

输出拦截机制：
  - 如果模型尝试直接回答用户但目标未完成，中间件会拦截并跳回模型重新生成
  - 连续拦截超过 3 次后，强制输出失败提示并终止
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT, hook_config
from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage
from langgraph.runtime import Runtime
from typing_extensions import override

from app.writer.tools import (
    RECORD_GOAL_COMPLETION_TOOL_DESCRIPTION,
    SET_GOAL_TOOL_DESCRIPTION,
    GoalState,
    build_goal_tools,
)

# 最大连续拦截次数：超过后强制终止并报告失败
_MAX_GOAL_OUTPUT_BLOCKS = 3
# 连续拦截达到上限时的失败提示
_GOAL_FAILURE_TEXT = (
    "Goal validation failed: the model attempted to produce a final answer 3 consecutive times "
    "before marking the goal as complete."
)


class GoalMiddleware(AgentMiddleware[GoalState[ResponseT], ContextT, ResponseT]):
    """写作目标工具注册 + 输出拦截中间件。

    通过 tools 通道注册 set_goal 和 record_goal_completion 工具，
    声明 GoalState 状态 schema，并在 after_model hook 中拦截未达标的最终回答。
    不修改 system prompt，保持缓存前缀稳定。
    """

    state_schema = GoalState  # type: ignore[assignment]

    def __init__(
        self,
        *,
        set_goal_description: str = SET_GOAL_TOOL_DESCRIPTION,
        record_completion_description: str = RECORD_GOAL_COMPLETION_TOOL_DESCRIPTION,
    ) -> None:
        super().__init__()
        self.tools = build_goal_tools(set_goal_description, record_completion_description)

    # ------------------------------------------------------------------
    # after_model hook：模型输出后拦截未达标的最终回答
    # ------------------------------------------------------------------

    @hook_config(can_jump_to=["model"])
    @override
    def after_model(self, state: GoalState[ResponseT], runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """同步：模型输出后的目标完成度检查。"""
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
    parallel_goal_error = _reject_parallel_goal_calls(state)
    if parallel_goal_error is not None:
        return parallel_goal_error

    messages = state.get("messages")
    if not messages:
        return None

    last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
    if not last_ai_msg or last_ai_msg.tool_calls:
        return None
    if _goal_completed_for_latest_user_turn(state):
        return None

    # 目标未完成，模型试图输出最终回答 → 拦截
    block_count = int(state.get("goal_output_block_count") or 0) + 1
    blocked_message = RemoveMessage(id=last_ai_msg.id) if last_ai_msg.id else None

    if block_count >= _MAX_GOAL_OUTPUT_BLOCKS:
        return {
            "goal_output_blocked": True,
            "goal_output_block_count": block_count,
            "messages": [message for message in [blocked_message, AIMessage(content=_GOAL_FAILURE_TEXT)] if message],
        }

    return {
        "jump_to": "model",
        "goal_output_blocked": True,
        "goal_output_block_count": block_count,
        "messages": [blocked_message] if blocked_message else [],
    }


def _reject_parallel_goal_calls(state: GoalState[ResponseT]) -> dict[str, Any] | None:
    """检查并拒绝并行调用多个目标工具。"""
    messages = state.get("messages")
    if not messages:
        return None

    last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
    if not last_ai_msg or not last_ai_msg.tool_calls:
        return None

    goal_calls = [tc for tc in last_ai_msg.tool_calls if tc["name"] in {"set_goal", "record_goal_completion"}]
    if len(goal_calls) <= 1:
        return None

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
    """检查目标是否在最新用户轮次中已完成。"""
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
    """判断消息是否为用户消息。"""
    if isinstance(message, Mapping):
        return message.get("role") == "user" or message.get("type") == "human"
    return getattr(message, "type", None) == "human" or getattr(message, "role", None) == "user"


__all__ = ["GoalMiddleware"]
