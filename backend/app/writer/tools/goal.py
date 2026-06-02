from collections.abc import Mapping
from typing import Annotated, Any

from langchain.agents.middleware.types import AgentState, ContextT, PrivateStateAttr, ResponseT
from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field
from typing_extensions import NotRequired


class GoalState(AgentState[ResponseT]):
    goal: Annotated[NotRequired[str], PrivateStateAttr]
    goal_completed: Annotated[NotRequired[bool | None], PrivateStateAttr]
    goal_acceptance_evidence: Annotated[NotRequired[str | None], PrivateStateAttr]
    goal_completed_for_turn: Annotated[NotRequired[int], PrivateStateAttr]
    goal_output_blocked: Annotated[NotRequired[bool], PrivateStateAttr]
    goal_output_block_count: Annotated[NotRequired[int], PrivateStateAttr]
    goal_updated_for_turn: Annotated[NotRequired[int], PrivateStateAttr]


class SetGoalInput(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    reason: str | None = Field(default=None, max_length=1000)


class RecordGoalCompletionInput(BaseModel):
    completed: bool
    acceptance_evidence: str = Field(min_length=1, max_length=4000)


SET_GOAL_TOOL_DESCRIPTION = """在用户输入后，用此工具确立或修改当前任务目标。

目标是从用户最新意图中提炼出的稳定任务目标，应覆盖用户真正要达成的结果、关键约束和验收重点，同时避免写成执行步骤或冗长实现细节。

## 何时使用
- 只能在用户输入后使用，用于确立新目标或根据用户最新输入修改目标。
- 每次用户输入最多调用一次。
- 不要因为工具输出、子代理输出或内部反思而修改目标。
- 如果现有目标仍匹配用户请求，不要调用此工具。

## 不要用于
- 不要用它记录验收证据。
- 不要用它修改完成状态。"""


RECORD_GOAL_COMPLETION_TOOL_DESCRIPTION = """在最终输出前，用此工具记录目标完成状态和验收证据。

验收证据用于说明最终结果是否符合当前目标，以及支撑该判断的具体观察结果。

## 何时使用
- 只能在准备最终回复用户前使用。
- 输出最终答复前必须调用此工具，并把 `completed` 设为 true，除非当前结果确实尚未完成。
- 如果 `completed` 为 false，继续补足工作，不要直接最终回复。

## 不要用于
- 不要用它确立或修改目标文本。
- 不要用它替代任务执行或子代理审查。"""


def build_goal_tools(
    set_goal_description: str = SET_GOAL_TOOL_DESCRIPTION,
    record_completion_description: str = RECORD_GOAL_COMPLETION_TOOL_DESCRIPTION,
) -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            name="set_goal",
            description=set_goal_description,
            func=set_goal,
            coroutine=aset_goal,
            args_schema=SetGoalInput,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="record_goal_completion",
            description=record_completion_description,
            func=record_goal_completion,
            coroutine=arecord_goal_completion,
            args_schema=RecordGoalCompletionInput,
            infer_schema=False,
        ),
    ]


def set_goal(
    runtime: ToolRuntime[ContextT, GoalState[ResponseT]],
    goal: str,
    reason: str | None = None,
) -> Command[Any]:
    cleaned_goal = goal.strip()
    if not cleaned_goal:
        return _goal_error(runtime.tool_call_id, "Goal cannot be empty.")

    current_turn = _current_user_turn(runtime.state.get("messages", []))
    if current_turn == 0:
        return _goal_error(runtime.tool_call_id, "The `set_goal` tool can only be used after a user input.")
    if runtime.state.get("goal_updated_for_turn") == current_turn:
        return _goal_error(
            runtime.tool_call_id,
            "The `set_goal` tool has already been used for this user input. Wait for the next user input before changing the goal again.",
        )
    current_goal = runtime.state.get("goal")
    if cleaned_goal == (current_goal.strip() if isinstance(current_goal, str) else None):
        return _goal_error(runtime.tool_call_id, "The existing goal already matches this text; do not call `set_goal` again.")

    content = f"Updated goal to: {cleaned_goal}"
    if reason and reason.strip():
        content = f"{content}\nReason: {reason.strip()}"

    return Command(
        update={
            "goal": cleaned_goal,
            "goal_completed": None,
            "goal_acceptance_evidence": None,
            "goal_completed_for_turn": None,
            "goal_output_blocked": False,
            "goal_output_block_count": 0,
            "goal_updated_for_turn": current_turn,
            "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
        }
    )


async def aset_goal(
    runtime: ToolRuntime[ContextT, GoalState[ResponseT]],
    goal: str,
    reason: str | None = None,
) -> Command[Any]:
    return set_goal(runtime, goal, reason)


def record_goal_completion(
    runtime: ToolRuntime[ContextT, GoalState[ResponseT]],
    completed: bool,
    acceptance_evidence: str,
) -> Command[Any]:
    current_goal = runtime.state.get("goal")
    if not isinstance(current_goal, str) or not current_goal.strip():
        return _goal_error(runtime.tool_call_id, "Set a goal before recording completion.")

    cleaned_evidence = acceptance_evidence.strip()
    if not cleaned_evidence:
        return _goal_error(runtime.tool_call_id, "Acceptance evidence is required.")

    current_turn = _current_user_turn(runtime.state.get("messages", []))
    if current_turn == 0:
        return _goal_error(runtime.tool_call_id, "The `record_goal_completion` tool can only be used after a user input.")

    status = "complete" if completed else "not complete"
    return Command(
        update={
            "goal_completed": completed,
            "goal_acceptance_evidence": cleaned_evidence,
            "goal_completed_for_turn": current_turn,
            "goal_output_blocked": False,
            "goal_output_block_count": 0,
            "messages": [
                ToolMessage(
                    content=f"Goal acceptance recorded: {status}\nEvidence: {cleaned_evidence}",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }
    )


async def arecord_goal_completion(
    runtime: ToolRuntime[ContextT, GoalState[ResponseT]],
    completed: bool,
    acceptance_evidence: str,
) -> Command[Any]:
    return record_goal_completion(runtime, completed, acceptance_evidence)


def _goal_error(tool_call_id: str | None, content: str) -> Command[Any]:
    return Command(update={"messages": [ToolMessage(content=content, tool_call_id=tool_call_id, status="error")]})


def _current_user_turn(messages: list[Any]) -> int:
    return sum(1 for message in messages if _is_user_message(message))


def _is_user_message(message: Any) -> bool:
    if isinstance(message, HumanMessage):
        return True
    if isinstance(message, Mapping):
        return message.get("role") == "user" or message.get("type") == "human"
    return getattr(message, "type", None) == "human" or getattr(message, "role", None) == "user"


__all__ = [
    "RECORD_GOAL_COMPLETION_TOOL_DESCRIPTION",
    "RecordGoalCompletionInput",
    "SET_GOAL_TOOL_DESCRIPTION",
    "SetGoalInput",
    "GoalState",
    "arecord_goal_completion",
    "aset_goal",
    "build_goal_tools",
    "record_goal_completion",
    "set_goal",
]
