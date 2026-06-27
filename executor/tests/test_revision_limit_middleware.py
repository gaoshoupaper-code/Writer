from __future__ import annotations

import unittest
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

# middleware 已迁进 harness 包（Phase 7），测试通过包加载后 import
from app.platform.agent.loader import load_current_package
load_current_package()
from harness_current.middleware.revision_limit import RevisionLimitMiddleware


def _request(
    tool_name: str,
    subagent_type: str | None = None,
    call_id: str = "call_1",
) -> SimpleNamespace:
    """构造模拟 ToolCallRequest：task 工具调用带 subagent_type 参数。"""
    args: dict = {}
    if subagent_type is not None:
        args["subagent_type"] = subagent_type
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": args, "id": call_id},
    )


class _CallTracker:
    """记录 handler 是否被调用 + 返回固定值，用于断言「放行 vs 拦截」。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: object) -> str:
        self.calls += 1
        return "passed-through"


class RevisionLimitMiddlewareTest(unittest.TestCase):
    def test_first_review_call_passes(self) -> None:
        mw = RevisionLimitMiddleware(max_revisions=1)
        tracker = _CallTracker()

        result = mw.wrap_tool_call(_request("task", "review"), tracker)

        self.assertEqual(result, "passed-through")
        self.assertEqual(mw._revision_count, 1)

    def test_second_review_call_is_blocked(self) -> None:
        mw = RevisionLimitMiddleware(max_revisions=1)
        tracker = _CallTracker()

        mw.wrap_tool_call(_request("task", "review", "c1"), tracker)
        # 第 2 次 review 调用 → 超限拦截，handler 不应被调用
        result = mw.wrap_tool_call(_request("task", "review", "c2"), tracker)

        self.assertIsInstance(result, ToolMessage)
        self.assertIn("审查上限", result.content)
        self.assertEqual(result.name, "task")
        self.assertEqual(tracker.calls, 1)
        self.assertEqual(mw._revision_count, 2)

    def test_non_review_task_passes_without_count(self) -> None:
        mw = RevisionLimitMiddleware(max_revisions=1)
        tracker = _CallTracker()

        result = mw.wrap_tool_call(_request("task", "other-subagent"), tracker)

        self.assertEqual(result, "passed-through")
        self.assertEqual(mw._revision_count, 0)

    def test_non_task_tool_passes_without_count(self) -> None:
        mw = RevisionLimitMiddleware(max_revisions=1)
        tracker = _CallTracker()

        result = mw.wrap_tool_call(_request("write_file"), tracker)

        self.assertEqual(result, "passed-through")
        self.assertEqual(mw._revision_count, 0)

    def test_before_agent_resets_count_for_new_invocation(self) -> None:
        """同一实例多调用周期：第 1 周期达上限被拦，before_agent 重置后第 2 周期重新放行。

        覆盖真实装配盲区——子代理 graph 会话内多次 task 复用同一实例，
        审查额度须按「每次子代理调用」重置（这正是线上「后续调用没审查」的根因）。
        """
        mw = RevisionLimitMiddleware(max_revisions=1)
        tracker = _CallTracker()

        # 调用周期 1（子代理被父 agent task 委托一次）
        mw.before_agent(state={}, runtime=None)
        mw.wrap_tool_call(_request("task", "review", "c1"), tracker)
        blocked = mw.wrap_tool_call(_request("task", "review", "c2"), tracker)
        self.assertIsInstance(blocked, ToolMessage)
        self.assertEqual(tracker.calls, 1)

        # 调用周期 2：重置后重新享有审查额度
        mw.before_agent(state={}, runtime=None)
        self.assertEqual(mw._revision_count, 0)
        result = mw.wrap_tool_call(_request("task", "review", "c3"), tracker)
        self.assertEqual(result, "passed-through")
        self.assertEqual(mw._revision_count, 1)
        self.assertEqual(tracker.calls, 2)


if __name__ == "__main__":
    unittest.main()
