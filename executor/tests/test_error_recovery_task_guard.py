"""ErrorRecoveryMiddleware task 防重放集成测试（CON-005 / FR-003 / DEC-003）。

验证 harness ErrorRecoveryMiddleware 接受平台注入的 TaskReplayPolicy 后：
  - task 工具失败精确执行 1 次（不重放整个子 Agent）；
  - 普通工具仍按既有预算重试。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# 直接 import 工作仓 harness 包（不依赖 .harness_checkout 生产快照）。
_HARNESS_REPO = Path(__file__).resolve().parents[2] / "evolution" / "harnesses" / "repo"
if str(_HARNESS_REPO) not in sys.path:
    sys.path.insert(0, str(_HARNESS_REPO))

from middleware.error_recovery import ErrorRecoveryMiddleware  # noqa: E402

from app.domains.writing.task_replay_policy import TaskReplayPolicy  # noqa: E402


def _req(tool_name: str) -> SimpleNamespace:
    return SimpleNamespace(tool_call={"id": "call-1", "name": tool_name, "args": {}})


class ErrorRecoveryTaskGuardTest(unittest.TestCase):
    def test_task_not_replayed_when_policy_injected(self) -> None:
        policy = TaskReplayPolicy()
        mw = ErrorRecoveryMiddleware(max_retries=2, tool_replay_policy=policy)
        calls = []

        def handler(_req):
            calls.append(1)
            raise RuntimeError("subagent internal timeout")

        result = mw.wrap_tool_call(_req("task"), handler)
        # task 失败精确 1 次，不被通用恢复重放（CON-005）。
        self.assertEqual(len(calls), 1)
        # 返回的是错误 ToolMessage（交回 Meta Agent 用新身份重新委派）。
        self.assertEqual(getattr(result, "status", None), "error")

    def test_normal_tool_still_retries(self) -> None:
        policy = TaskReplayPolicy()
        mw = ErrorRecoveryMiddleware(max_retries=2, retry_delay=0, tool_replay_policy=policy)
        calls = []

        def handler(_req):
            calls.append(1)
            if len(calls) < 2:
                raise FileNotFoundError("missing")
            return "ok"

        result = mw.wrap_tool_call(_req("read_file"), handler)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 2)

    def test_no_policy_keeps_legacy_behavior(self) -> None:
        # 不注入 policy 时（旧 snapshot / 未升级路径）保持既有重试行为。
        mw = ErrorRecoveryMiddleware(max_retries=1, retry_delay=0)
        calls = []

        def handler(_req):
            calls.append(1)
            raise RuntimeError("err")

        mw.wrap_tool_call(_req("task"), handler)
        # 无 policy：task 仍按既有预算重试（向后兼容，不破坏未升级 snapshot）。
        self.assertEqual(len(calls), 2)

    def test_async_task_not_replayed(self) -> None:
        policy = TaskReplayPolicy()
        mw = ErrorRecoveryMiddleware(max_retries=2, retry_delay=0, tool_replay_policy=policy)
        calls = []

        async def handler(_req):
            calls.append(1)
            raise RuntimeError("subagent timeout")

        async def run():
            return await mw.awrap_tool_call(_req("task"), handler)

        result = asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertEqual(getattr(result, "status", None), "error")


if __name__ == "__main__":
    unittest.main()
