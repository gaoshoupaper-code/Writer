from __future__ import annotations

import unittest
from typing import get_type_hints

from langchain.agents.factory import _resolve_schemas
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain.tools import ToolRuntime

from app.domains.writing.middleware.goal_middleware import GoalMiddleware
from app.domains.writing.tools.goal import GoalState, aset_goal, set_goal


class GoalMiddlewareTest(unittest.TestCase):
    def test_goal_tool_updates_state_once_per_user_turn(self) -> None:
        first_runtime = _tool_runtime(
            state={"messages": [HumanMessage(content="write a sci-fi story")]},
            tool_call_id="goal-1",
        )

        first = set_goal(first_runtime, "Write a sci-fi story about identity.", "User request")

        update = first.update
        self.assertEqual(update["goal"], "Write a sci-fi story about identity.")
        self.assertEqual(update["goal_updated_for_turn"], 1)
        self.assertIsInstance(update["messages"][0], ToolMessage)
        self.assertEqual(update["messages"][0].status, "success")

        second_runtime = _tool_runtime(
            state={
                "messages": [HumanMessage(content="write a sci-fi story"), update["messages"][0]],
                "goal": update["goal"],
                "goal_updated_for_turn": update["goal_updated_for_turn"],
            },
            tool_call_id="goal-2",
        )
        second = set_goal(second_runtime, "Change it into a cyberpunk story.")

        self.assertEqual(second.update["messages"][0].status, "error")
        self.assertIn("already been used", second.update["messages"][0].content)

    def test_goal_tool_allows_update_after_next_user_turn(self) -> None:
        runtime = _tool_runtime(
            state={
                "messages": [
                    HumanMessage(content="write a sci-fi story"),
                    AIMessage(content="ok"),
                    HumanMessage(content="change it into a mystery"),
                ],
                "goal": "Write a sci-fi story about identity.",
                "goal_updated_for_turn": 1,
            },
            tool_call_id="goal-2",
        )

        result = set_goal(runtime, "Write a mystery story.")

        self.assertEqual(result.update["goal"], "Write a mystery story.")
        self.assertEqual(result.update["goal_updated_for_turn"], 2)

    def test_goal_tool_rejects_before_user_input(self) -> None:
        runtime = _tool_runtime(state={"messages": []}, tool_call_id="goal-1")

        result = set_goal(runtime, "Write a story.")

        self.assertEqual(result.update["messages"][0].status, "error")
        self.assertIn("after a user input", result.update["messages"][0].content)

    def test_after_model_rejects_parallel_goal_calls(self) -> None:
        middleware = GoalMiddleware()
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "set_goal", "args": {"goal": "A"}, "id": "call-1"},
                        {"name": "set_goal", "args": {"goal": "B"}, "id": "call-2"},
                    ],
                )
            ]
        }

        result = middleware.after_model(state, runtime=None)  # type: ignore[arg-type]

        self.assertIsNotNone(result)
        messages = result["messages"]
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(message.status == "error" for message in messages))

    def test_after_model_blocks_final_output_until_goal_completed(self) -> None:
        middleware = GoalMiddleware()
        state = {
            "messages": [HumanMessage(content="write a story"), AIMessage(content="Done", id="ai-1")],
            "goal": "Write a story.",
            "goal_completed": False,
        }

        result = middleware.after_model(state, runtime=None)  # type: ignore[arg-type]

        self.assertIsNotNone(result)
        self.assertEqual(result["jump_to"], "model")
        self.assertTrue(result["goal_output_blocked"])
        self.assertEqual(result["goal_output_block_count"], 1)
        self.assertEqual(result["messages"][0].id, "ai-1")

    def test_after_model_fails_after_three_consecutive_goal_blocks(self) -> None:
        middleware = GoalMiddleware()
        state = {
            "messages": [HumanMessage(content="write a story"), AIMessage(content="Done", id="ai-3")],
            "goal": "Write a story.",
            "goal_completed": False,
            "goal_output_block_count": 2,
        }

        result = middleware.after_model(state, runtime=None)  # type: ignore[arg-type]

        self.assertIsNotNone(result)
        self.assertNotIn("jump_to", result)
        self.assertTrue(result["goal_output_blocked"])
        self.assertEqual(result["goal_output_block_count"], 3)
        self.assertEqual(result["messages"][0].id, "ai-3")
        self.assertIsInstance(result["messages"][1], AIMessage)
        self.assertIn("3 consecutive times", result["messages"][1].content)

    def test_after_model_allows_final_output_when_goal_completed(self) -> None:
        middleware = GoalMiddleware()
        state = {
            "messages": [HumanMessage(content="write a story"), AIMessage(content="Done", id="ai-1")],
            "goal": "Write a story.",
            "goal_completed": True,
            "goal_acceptance_evidence": "Final review passed.",
            "goal_completed_for_turn": 1,
        }

        result = middleware.after_model(state, runtime=None)  # type: ignore[arg-type]

        self.assertIsNone(result)

    def test_after_model_allows_tool_calls_before_goal_completed(self) -> None:
        middleware = GoalMiddleware()
        state = {
            "messages": [
                HumanMessage(content="write a story"),
                AIMessage(content="", tool_calls=[{"name": "goal", "args": {"goal": "Write a story."}, "id": "goal-1"}]),
            ],
            "goal_completed": False,
        }

        result = middleware.after_model(state, runtime=None)  # type: ignore[arg-type]

        self.assertIsNone(result)

    def test_goal_state_is_omitted_from_agent_output_schema(self) -> None:
        _, _, output_schema = _resolve_schemas([GoalState])
        output_hints = get_type_hints(output_schema, include_extras=True)

        self.assertNotIn("goal", output_hints)
        self.assertNotIn("goal_completed", output_hints)
        self.assertNotIn("goal_acceptance_evidence", output_hints)
        self.assertNotIn("goal_output_block_count", output_hints)
        self.assertNotIn("goal_updated_for_turn", output_hints)
        self.assertIn("messages", output_hints)

    def test_goal_tool_resets_acceptance_when_objective_changes(self) -> None:
        runtime = _tool_runtime(
            state={
                "messages": [HumanMessage(content="write a sci-fi story"), HumanMessage(content="make it a mystery")],
                "goal": "Write a sci-fi story about identity.",
                "goal_completed": True,
                "goal_acceptance_evidence": "Final review passed.",
                "goal_updated_for_turn": 1,
            },
            tool_call_id="goal-2",
        )

        result = set_goal(runtime, "Write a mystery story.")

        update = result.update
        self.assertEqual(update["goal"], "Write a mystery story.")
        self.assertIsNone(update["goal_completed"])
        self.assertIsNone(update["goal_acceptance_evidence"])
        self.assertFalse(update["goal_output_blocked"])
        self.assertEqual(update["goal_output_block_count"], 0)


def _tool_runtime(state: dict, tool_call_id: str) -> ToolRuntime:
    return ToolRuntime(
        state=state,
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id=tool_call_id,
        store=None,
        tools=[],
    )


if __name__ == "__main__":
    unittest.main()
