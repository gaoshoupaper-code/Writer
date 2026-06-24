from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

# StorylineSingleLineLimitMiddleware 已迁进 harness 包（Phase 7），通过包加载后 import
from app.platform.agent.loader import load_current_package
load_current_package()
from harness_current.middleware.storyline_single_line_limit import (
    StorylineSingleLineLimitMiddleware,
)


def _request(tool_name: str, file_path: str, call_id: str = "call_1") -> SimpleNamespace:
    """构造模拟 ToolCallRequest：tool_call 为 dict（与真实请求结构一致）。"""
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": {"file_path": file_path}, "id": call_id},
    )


class _CallTracker:
    """记录 handler 是否被调用 + 返回固定值，用于断言「放行 vs 拦截」。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: object) -> str:
        self.calls += 1
        return "passed-through"


class StorylineSingleLineLimitMiddlewareTest(unittest.TestCase):
    def test_first_new_storyline_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mw = StorylineSingleLineLimitMiddleware(Path(tmpdir), max_new_lines=1)
            tracker = _CallTracker()

            result = mw.wrap_tool_call(_request("write_file", "/storyline/S02-主线.md"), tracker)

            self.assertEqual(result, "passed-through")
            self.assertEqual(tracker.calls, 1)
            self.assertEqual(mw._new_line_count, 1)

    def test_second_new_storyline_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mw = StorylineSingleLineLimitMiddleware(Path(tmpdir), max_new_lines=1)
            tracker = _CallTracker()

            mw.wrap_tool_call(_request("write_file", "/storyline/S02-主线.md", "c1"), tracker)
            # 第 2 条不同文件 → 新增 → 超限拦截，handler 不应被调用
            result = mw.wrap_tool_call(_request("write_file", "/storyline/S03-支线.md", "c2"), tracker)

            self.assertIsInstance(result, ToolMessage)
            self.assertIn("上限", result.content)
            self.assertEqual(result.name, "write_file")
            self.assertEqual(tracker.calls, 1)
            self.assertEqual(mw._new_line_count, 2)

    def test_write_to_existing_storyline_passes_without_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "storyline").mkdir()
            (workspace / "storyline" / "S02-主线.md").write_text("已存在", encoding="utf-8")

            mw = StorylineSingleLineLimitMiddleware(workspace, max_new_lines=1)
            tracker = _CallTracker()

            result = mw.wrap_tool_call(_request("write_file", "/storyline/S02-主线.md"), tracker)

            self.assertEqual(result, "passed-through")
            self.assertEqual(mw._new_line_count, 0)

    def test_edit_file_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mw = StorylineSingleLineLimitMiddleware(Path(tmpdir), max_new_lines=1)
            tracker = _CallTracker()

            result = mw.wrap_tool_call(_request("edit_file", "/storyline/S02-主线.md"), tracker)

            self.assertEqual(result, "passed-through")
            self.assertEqual(mw._new_line_count, 0)

    def test_non_storyline_write_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mw = StorylineSingleLineLimitMiddleware(Path(tmpdir), max_new_lines=1)
            tracker = _CallTracker()

            result = mw.wrap_tool_call(_request("write_file", "/worldview.md"), tracker)

            self.assertEqual(result, "passed-through")
            self.assertEqual(mw._new_line_count, 0)

    def test_non_write_file_tool_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mw = StorylineSingleLineLimitMiddleware(Path(tmpdir), max_new_lines=1)
            tracker = _CallTracker()

            result = mw.wrap_tool_call(_request("read_file", "/storyline/S02-主线.md"), tracker)

            self.assertEqual(result, "passed-through")
            self.assertEqual(mw._new_line_count, 0)

    def test_timeline_md_not_counted_as_storyline(self) -> None:
        """timeline.md 是全局时间线表，不是故事线——写它不应计入单线计数。

        复现初构真实场景：agent 先写主线 S01-{名}.md，再写 timeline.md，
        两者都应放行，且计数仍为 1（timeline 不占新增故事线额度）。
        见需求基准 D3/D4。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            mw = StorylineSingleLineLimitMiddleware(Path(tmpdir), max_new_lines=1)
            tracker = _CallTracker()

            # 写主线（第 1 条故事线）→ 计数 1，放行
            r1 = mw.wrap_tool_call(_request("write_file", "/storyline/S01-成长主线.md"), tracker)
            self.assertEqual(r1, "passed-through")
            self.assertEqual(mw._new_line_count, 1)

            # 写 timeline.md → 非故事线，不计入，放行
            r2 = mw.wrap_tool_call(_request("write_file", "/storyline/timeline.md"), tracker)
            self.assertEqual(r2, "passed-through")
            self.assertEqual(mw._new_line_count, 1)  # 计数不增长

    def test_before_agent_resets_count_for_new_invocation(self) -> None:
        """同一实例多调用周期：第 1 周期达上限被拦，before_agent 重置后第 2 周期重新放行。

        覆盖真实装配盲区——子代理 graph 一次编译、会话内多次 task 复用同一中间件实例，
        计数须按「每次子代理调用」重置，而非跨调用累积（这正是线上 bug 的根因）。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            mw = StorylineSingleLineLimitMiddleware(Path(tmpdir), max_new_lines=1)
            tracker = _CallTracker()

            # 调用周期 1（storybuilding 被父 agent task 委托一次）
            mw.before_agent(state={}, runtime=None)
            mw.wrap_tool_call(_request("write_file", "/storyline/S01-主线.md"), tracker)
            blocked = mw.wrap_tool_call(_request("write_file", "/storyline/S02-支线.md"), tracker)
            self.assertIsInstance(blocked, ToolMessage)
            self.assertEqual(mw._new_line_count, 2)
            self.assertEqual(tracker.calls, 1)  # 仅第 1 次真正放行到 handler

            # 调用周期 2（再次委托）：before_agent 已重置，重新享有额度
            mw.before_agent(state={}, runtime=None)
            self.assertEqual(mw._new_line_count, 0)
            result = mw.wrap_tool_call(_request("write_file", "/storyline/S03-支线.md"), tracker)
            self.assertEqual(result, "passed-through")
            self.assertEqual(mw._new_line_count, 1)
            self.assertEqual(tracker.calls, 2)


class _AsyncCallTracker:
    """异步 handler 记录器，用于断言 awrap_tool_call 的放行/拦截。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request: object) -> str:
        self.calls += 1
        return "passed-through"


class StorylineSingleLineLimitMiddlewareAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_abefore_agent_resets_count_for_new_invocation(self) -> None:
        """异步路径（generate_stream → ainvoke → abefore_agent）：同样按调用周期重置。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            mw = StorylineSingleLineLimitMiddleware(Path(tmpdir), max_new_lines=1)
            tracker = _AsyncCallTracker()

            await mw.abefore_agent(state={}, runtime=None)
            await mw.awrap_tool_call(_request("write_file", "/storyline/S01.md"), tracker)
            blocked = await mw.awrap_tool_call(_request("write_file", "/storyline/S02.md"), tracker)
            self.assertIsInstance(blocked, ToolMessage)

            await mw.abefore_agent(state={}, runtime=None)
            self.assertEqual(mw._new_line_count, 0)
            result = await mw.awrap_tool_call(_request("write_file", "/storyline/S03.md"), tracker)
            self.assertEqual(result, "passed-through")
            self.assertEqual(tracker.calls, 2)


if __name__ == "__main__":
    unittest.main()
