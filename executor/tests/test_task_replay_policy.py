"""TaskReplayPolicy —— 平台级 task 防重放边界测试（CON-005 / FR-003 / DEC-003）。"""
from __future__ import annotations

import unittest

from app.domains.writing.task_replay_policy import POLICY_VERSION, TaskReplayPolicy


class TaskReplayPolicyTest(unittest.TestCase):
    def test_task_tool_never_retried(self) -> None:
        policy = TaskReplayPolicy()
        # task 运行整个子 Agent，重放会重复副作用/计费，恒不可重试。
        self.assertFalse(policy.should_retry("task", Exception("timeout")))
        self.assertFalse(policy.should_retry("task", TimeoutError()))

    def test_other_tools_retryable(self) -> None:
        policy = TaskReplayPolicy()
        self.assertTrue(policy.should_retry("read_file", Exception()))
        self.assertTrue(policy.should_retry("edit_file", Exception()))
        self.assertTrue(policy.should_retry("write_file", Exception()))

    def test_replay_blocked_callback_fires_only_for_task(self) -> None:
        blocked: list[tuple] = []
        policy = TaskReplayPolicy(on_replay_blocked=lambda name, ver, exc: blocked.append((name, ver)))
        policy.should_retry("read_file", ValueError("x"))
        self.assertEqual(blocked, [])
        policy.should_retry("task", ValueError("x"))
        self.assertEqual(blocked, [("task", POLICY_VERSION)])

    def test_callback_failure_does_not_break_decision(self) -> None:
        def boom(name, ver, exc):
            raise RuntimeError("observer down")

        policy = TaskReplayPolicy(on_replay_blocked=boom)
        # 观测回调失败不得改变判定（仍 False）。
        self.assertFalse(policy.should_retry("task", Exception()))

    def test_empty_or_none_tool_name_treated_safely(self) -> None:
        policy = TaskReplayPolicy()
        # 缺名工具不被当成 task，保持可重试（保守不误伤）。
        self.assertTrue(policy.should_retry(None, Exception()))
        self.assertTrue(policy.should_retry("", Exception()))


if __name__ == "__main__":
    unittest.main()
