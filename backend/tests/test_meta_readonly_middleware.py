from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import ToolMessage

from app.writer.middleware.meta_readonly_middleware import (
    MetaReadOnlyMiddleware,
    _resolve_subagent,
)


def _request(tool_name: str, file_path: str = "", tool_call_id: str = "call-1") -> Any:
    """构造带 tool_call 属性的伪 request，模拟 DeepAgents 中间件入参。"""
    args: dict[str, Any] = {}
    if file_path:
        args["file_path"] = file_path
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": args, "id": tool_call_id}
    )


class MetaReadOnlyMiddlewareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mw = MetaReadOnlyMiddleware()

    def _assert_blocked(self, tool_name: str, file_path: str, expected_subagent: str) -> ToolMessage:
        """断言写入工具被拦截：返回 error ToolMessage、handler 未被调用、content 含对应子代理。"""
        handler_calls = {"n": 0}

        def handler(_req: Any) -> Any:
            handler_calls["n"] += 1
            return "should-not-reach"

        result = self.mw.wrap_tool_call(_request(tool_name, file_path), handler)
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertEqual(handler_calls["n"], 0)
        assert isinstance(result.content, str)
        self.assertIn(expected_subagent, result.content)
        return result

    def test_blocks_write_and_edit_tools(self) -> None:
        for tool_name in ("write_file", "edit_file"):
            with self.subTest(tool=tool_name):
                self._assert_blocked(tool_name, "/demand.md", "interview")

    def test_routes_each_path_prefix_to_correct_subagent(self) -> None:
        cases = [
            ("/demand.md", "interview"),
            ("/character/林映真.md", "storybuilding"),
            ("/outline.md", "storybuilding"),
            ("/storyline.md", "storybuilding"),
            ("/storyline/主线.md", "storybuilding"),
            ("/worldview.md", "storybuilding"),
            ("/evaluation.md", "storybuilding"),
            ("/detail/chapter-01.md", "detail-outline"),
            ("/chapter/chapter-01.md", "writing"),
            ("/novel.md", "writing"),
            ("/state_log.md", "writing"),
            ("/review/chapter-01.md", "writing"),
        ]
        for file_path, expected in cases:
            with self.subTest(path=file_path):
                self._assert_blocked("write_file", file_path, expected)

    def test_unknown_path_still_blocked_with_generic_guidance(self) -> None:
        handler_calls = {"n": 0}

        def handler(_req: Any) -> Any:
            handler_calls["n"] += 1
            return "should-not-reach"

        result = self.mw.wrap_tool_call(_request("write_file", "/something-else.md"), handler)
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertEqual(handler_calls["n"], 0)
        assert isinstance(result.content, str)
        # 通用引导列出全部子代理，便于 Meta 自行判断
        for sub in ("interview", "storybuilding", "detail-outline", "writing"):
            self.assertIn(sub, result.content)

    def test_relative_and_backslash_paths_normalized(self) -> None:
        self._assert_blocked("write_file", "demand.md", "interview")
        self._assert_blocked("write_file", r"detail\chapter-01.md", "detail-outline")

    def test_missing_file_path_still_blocked(self) -> None:
        # 即使工具未提供 file_path，写工具仍被拦截
        result = self.mw.wrap_tool_call(_request("write_file", ""), lambda _: "x")
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")

    def test_non_write_tools_pass_through(self) -> None:
        sentinel = {"ok": True}
        for tool_name in ("read_file", "task", "set_goal", "record_goal_completion"):
            with self.subTest(tool=tool_name):
                handler_calls = {"n": 0}

                def handler(_req: Any) -> Any:
                    handler_calls["n"] += 1
                    return sentinel

                result = self.mw.wrap_tool_call(_request(tool_name, "/demand.md"), handler)
                self.assertIs(result, sentinel)
                self.assertEqual(handler_calls["n"], 1)

    def test_async_write_blocked_without_calling_handler(self) -> None:
        async def must_not_call(_req: Any) -> Any:
            raise AssertionError("async handler must not be called for write tools")

        result = asyncio.run(
            self.mw.awrap_tool_call(_request("write_file", "/demand.md"), must_not_call)
        )
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")

    def test_async_read_passes_through(self) -> None:
        async def read_handler(_req: Any) -> Any:
            return "read-ok"

        result = asyncio.run(
            self.mw.awrap_tool_call(_request("read_file", "/demand.md"), read_handler)
        )
        self.assertEqual(result, "read-ok")


class ResolveSubagentTest(unittest.TestCase):
    def test_known_prefixes_and_unknown(self) -> None:
        self.assertEqual(_resolve_subagent("/demand.md"), "interview（需求分析）")
        self.assertEqual(_resolve_subagent("/chapter/chapter-01.md"), "writing（正文写作）")
        self.assertEqual(_resolve_subagent("/unknown.md"), "")
        self.assertEqual(_resolve_subagent(""), "")


if __name__ == "__main__":
    unittest.main()
