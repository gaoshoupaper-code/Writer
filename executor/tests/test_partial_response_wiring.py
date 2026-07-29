"""_track_partial_response —— 部分响应检测的生产接线测试（DEC-006 / EDGE-005 / AC-008）。

验证 retry runner factory 把 invoke 包了一层后，"已收到响应增量后中断"的流式失败
被正确识别为部分响应，从而禁止自动重放（不重复副作用/计费）。
"""
from __future__ import annotations

import asyncio
import unittest

from app.domains.writing.agent import _track_partial_response


class _FakeResponse:
    def __init__(self, body: dict | None, text: str | None = None) -> None:
        self._body = body
        self.text = text

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _PartialError(Exception):
    def __init__(self, msg: str, response=None) -> None:
        super().__init__(msg)
        self.response = response


class TrackPartialResponseTest(unittest.TestCase):
    def test_successful_invoke_is_not_partial(self) -> None:
        async def good_invoke():
            return {"messages": ["ok"]}

        tracked = _track_partial_response(good_invoke)
        result = asyncio.run(tracked.invoke())
        self.assertEqual(result, {"messages": ["ok"]})

    def test_stream_broke_after_choices_is_partial(self) -> None:
        # 流式已产出 choices 后断流：异常携带含 choices 的 response body。
        async def breaking_invoke():
            raise _PartialError(
                "stream interrupted",
                response=_FakeResponse({"choices": [{"message": {"content": "部分输出"}}]}),
            )

        tracked = _track_partial_response(breaking_invoke)
        with self.assertRaises(_PartialError) as ctx:
            asyncio.run(tracked.invoke())
        # 识别为部分响应 → 控制器应禁止重放。
        self.assertTrue(tracked.is_partial(ctx.exception))

    def test_stream_broke_after_usage_is_partial(self) -> None:
        async def breaking_invoke():
            raise _PartialError(
                "stream interrupted",
                response=_FakeResponse({"usage": {"prompt_tokens": 10}}),
            )

        tracked = _track_partial_response(breaking_invoke)
        with self.assertRaises(_PartialError) as ctx:
            asyncio.run(tracked.invoke())
        self.assertTrue(tracked.is_partial(ctx.exception))

    def test_connection_failure_before_any_chunk_is_not_partial(self) -> None:
        # 连接失败（无任何响应增量）：可重试，不算部分响应。
        async def failing_invoke():
            raise _PartialError("connection refused")  # 无 response body

        tracked = _track_partial_response(failing_invoke)
        with self.assertRaises(_PartialError) as ctx:
            asyncio.run(tracked.invoke())
        self.assertFalse(tracked.is_partial(ctx.exception))

    def test_error_with_empty_body_is_not_partial(self) -> None:
        async def failing_invoke():
            raise _PartialError("server error", response=_FakeResponse({"error": "5xx"}))

        tracked = _track_partial_response(failing_invoke)
        with self.assertRaises(_PartialError) as ctx:
            asyncio.run(tracked.invoke())
        # body 既无 choices 也无 usage → 视为尚未收到增量，可重试。
        self.assertFalse(tracked.is_partial(ctx.exception))


if __name__ == "__main__":
    unittest.main()
