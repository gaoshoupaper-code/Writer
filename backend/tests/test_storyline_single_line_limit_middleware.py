from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from app.writer.expert_agent.middleware.storyline_single_line_limit import (
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


if __name__ == "__main__":
    unittest.main()
