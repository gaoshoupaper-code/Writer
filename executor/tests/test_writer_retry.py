"""WriterRetryController —— 重试预算与错误分类的故障注入测试。

覆盖 FR-003 / NFR-002 / DEC-003 / DEC-006 / EDGE-002 / EDGE-005 的外部可观察契约：
  - 单一权威预算：总传输尝试数精确不超过 2（AC-005）。
  - 错误分类矩阵：可重试 vs 不可重试 vs 部分响应（AC-008）。
  - 部分响应/取消/认证绝不自动重放（EDGE-005/EDGE-004）。
  - 退避与 attempt 回调可观测（供 Trace 写 attempt 事件）。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.domains.writing.retry import (
    MAX_TRANSMIT_ATTEMPTS,
    AttemptOutcome,
    RetryBudget,
    WriterRetryController,
    WriterRetryError,
    classify_error,
)


def _err(name: str, *, status: int | None = None, response=None, partial: bool = False) -> BaseException:
    """构造一个按类名匹配的异常实例（模拟 openai 异常族，不硬依赖供应商）。"""
    exc = type(name, (Exception,), {})("boom")
    if status is not None:
        exc.status_code = status
        exc.response = response or SimpleNamespace(status_code=status)
    if partial:
        exc._writer_partial_response = True
    return exc


class ClassifyErrorTest(unittest.TestCase):
    def test_connection_and_timeout_are_retryable(self) -> None:
        self.assertEqual(classify_error(_err("APIConnectionError")), ("connection", True))
        self.assertEqual(classify_error(_err("APITimeoutError")), ("no_response", True))

    def test_rate_limit_and_5xx_are_retryable(self) -> None:
        self.assertEqual(classify_error(_err("RateLimitError")), ("rate_limited", True))
        self.assertEqual(classify_error(_err("APIStatusError", status=503)), ("server_error", True))

    def test_auth_bad_request_content_policy_not_retryable(self) -> None:
        self.assertEqual(classify_error(_err("AuthenticationError")), ("auth", False))
        self.assertEqual(classify_error(_err("PermissionDeniedError")), ("auth", False))
        self.assertEqual(classify_error(_err("BadRequestError")), ("bad_request", False))
        self.assertEqual(classify_error(_err("ContentPolicyViolationError")), ("content_policy", False))

    def test_4xx_status_not_retryable(self) -> None:
        self.assertEqual(classify_error(_err("APIStatusError", status=404)), ("http_status", False))

    def test_cancel_and_interrupt_not_retryable(self) -> None:
        self.assertEqual(classify_error(asyncio.CancelledError()), ("cancel", False))
        gi = type("GraphInterrupt", (BaseException,), {})("x")
        self.assertEqual(classify_error(gi), ("interrupt", False))


class RetryBudgetTest(unittest.TestCase):
    def _budget(self) -> tuple[RetryBudget, list[AttemptOutcome], list[tuple]]:
        outcomes: list[AttemptOutcome] = []
        backoffs: list[tuple] = []
        budget = RetryBudget(
            max_attempts=MAX_TRANSMIT_ATTEMPTS,
            backoff_seconds=0,
            on_attempt_complete=outcomes.append,
            on_backoff=lambda aid, att, delay: backoffs.append((aid, att, delay)),
            sleep=lambda _d: None,  # 虚拟时钟：退避 no-op
        )
        return budget, outcomes, backoffs

    def test_retryable_failure_retries_once_then_succeeds(self) -> None:
        budget, outcomes, backoffs = self._budget()
        calls = []

        def invoke():
            calls.append(1)
            if len(calls) < 2:
                raise _err("APITimeoutError")
            return "ok"

        result = WriterRetryController(budget).call(invoke)
        self.assertEqual(result, "ok")
        # 精确 2 次传输尝试，1 次退避（首次失败后）。
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(backoffs), 1)
        self.assertEqual([o.attempt_number for o in outcomes], [1, 2])
        self.assertTrue(outcomes[0].is_retryable and not outcomes[0].success)
        self.assertTrue(outcomes[1].success)

    def test_total_attempts_capped_at_two_on_persistent_no_response(self) -> None:
        budget, outcomes, backoffs = self._budget()
        calls = []

        def invoke():
            calls.append(1)
            raise _err("APITimeoutError")

        with self.assertRaises(WriterRetryError) as ctx:
            WriterRetryController(budget).call(invoke)
        # AC-005：总传输尝试数精确为 2，不与任何层相乘。
        self.assertEqual(len(calls), 2)
        self.assertEqual(ctx.exception.attempts_made, 2)
        self.assertTrue(ctx.exception.is_retryable)
        self.assertFalse(ctx.exception.had_partial_response)

    def test_non_retryable_error_executes_exactly_once(self) -> None:
        budget, outcomes, _ = self._budget()
        calls = []

        def invoke():
            calls.append(1)
            raise _err("AuthenticationError")

        with self.assertRaises(WriterRetryError) as ctx:
            WriterRetryController(budget).call(invoke)
        # AC-008：认证错误 attempt 精确为 1。
        self.assertEqual(len(calls), 1)
        self.assertEqual(ctx.exception.attempts_made, 1)
        self.assertFalse(ctx.exception.is_retryable)

    def test_partial_response_never_replayed(self) -> None:
        budget, outcomes, _ = self._budget()
        calls = []

        def invoke():
            calls.append(1)
            # 流式过程中已收到首块后断流：标记部分响应。
            raise _err("APIConnectionError", partial=True)

        with self.assertRaises(WriterRetryError) as ctx:
            WriterRetryController(budget).call(invoke)
        # EDGE-005/DEC-006：即便连接错误本可重试，部分响应也禁止重放 → 精确 1 次。
        self.assertEqual(len(calls), 1)
        self.assertTrue(ctx.exception.had_partial_response)
        self.assertFalse(ctx.exception.is_retryable)

    def test_cancel_propagates_without_retry(self) -> None:
        budget, _outcomes, _ = self._budget()
        calls = []

        def invoke():
            calls.append(1)
            raise asyncio.CancelledError()

        # 取消直接向上抛，不被吞成结构化重试错误（EDGE-004）。
        with self.assertRaises(asyncio.CancelledError):
            WriterRetryController(budget).call(invoke)
        self.assertEqual(len(calls), 1)

    def test_attempt_outcome_carries_identity_and_budget(self) -> None:
        budget, outcomes, _ = self._budget()
        same_id = []

        def invoke():
            raise _err("APITimeoutError")

        with self.assertRaises(WriterRetryError):
            WriterRetryController(budget).call(invoke)
        # 两次 attempt 共享同一逻辑调用身份。
        ids = {o.attempt_id for o in outcomes}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(outcomes), 2)


class AsyncRetryBudgetTest(unittest.TestCase):
    def test_async_retryable_then_success(self) -> None:
        outcomes: list[AttemptOutcome] = []
        backoffs = []
        budget = RetryBudget(
            on_attempt_complete=outcomes.append,
            on_backoff=lambda aid, att, d: backoffs.append((aid, att)),
            sleep_async=MagicMock(side_effect=lambda d: asyncio.sleep(0)),
        )
        calls = []

        async def invoke():
            calls.append(1)
            if len(calls) < 2:
                raise _err("APIConnectionError")
            return "ok"

        result = asyncio.run(WriterRetryController(budget).acall(invoke))
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual([o.attempt_number for o in outcomes], [1, 2])
        self.assertEqual(len(backoffs), 1)

    def test_async_partial_response_blocks_retry(self) -> None:
        budget = RetryBudget(sleep_async=MagicMock(side_effect=lambda d: asyncio.sleep(0)))
        calls = []

        async def invoke():
            calls.append(1)
            raise _err("APITimeoutError", partial=True)

        with self.assertRaises(WriterRetryError) as ctx:
            asyncio.run(WriterRetryController(budget).acall(invoke))
        self.assertEqual(len(calls), 1)
        self.assertTrue(ctx.exception.had_partial_response)


if __name__ == "__main__":
    unittest.main()
