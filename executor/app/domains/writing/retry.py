"""WriterRetryController —— 写作模型传输层的单一权威重试预算。

根治 EVD-003/004/005 的"重试相乘"根因：
  - SDK 层：``build_writer_model`` 显式 ``max_retries=0``，关闭 SDK 内部隐藏重试，
    让重试预算集中在本控制器，行为不再随第三方默认值漂移（CON-003）。
  - 模型层：本控制器对一次逻辑模型调用最多发起 2 次传输尝试（DEC-003：首次失败后
    最多自动重试 1 次），并对外产出可观测的 attempt 进展。
  - task 层：``task``（子 Agent 委派）整任务不进入通用工具恢复重放，由
    ``TaskReplayGuard`` 在 harness ErrorRecovery 处拦截（CON-005）。

可观测契约（FR-003/NFR-002）：
  - 每次 attempt 有稳定身份（``attempt_id``），记录开始/失败/退避/剩余预算/最终处置。
  - attempt 事件由调用方（TraceMiddleware / 模型中间件）写入 Trace；本控制器只回放
    结构化的 attempt 结果，不直接碰 recorder，保持单一职责。

重试分类（DEC-006/EDGE-005）——只有"尚无任何响应增量/工具调用/产物副作用"的短暂
依赖故障才允许进入第 2 次 attempt：
  可重试：连接失败、连续无响应超时（APITimeoutError）、限流、5xx（服务端临时故障）。
  不可重试：认证/权限、请求参数、内容策略、用户取消，以及"已收到任意流式增量后中断"
           的部分响应——后者直接返回结构化的部分响应错误，不自动重放。

健康长生成（EDGE-001）：245 秒预算只约束两次"连续无响应"的失败链路；持续收到流式
进展的调用不受 wall-clock 总限制，因此不会误杀正常长生成（RSK-001）。
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("writer.retry")

# DEC-003：首次失败后最多自动重试 1 次 → 总传输尝试数精确为 2。
MAX_TRANSMIT_ATTEMPTS = 2


class WriterRetryError(Exception):
    """模型传输预算用尽或不可重试时，向上层返回的结构化失败。

    携带逻辑调用身份、attempt 计数、错误分类、是否出现部分响应与已知 usage，
    供 Meta Agent / Trace / 计费各自按既有契约处置（FR-003 失败语义 / CON-004）。
    """

    def __init__(
        self,
        *,
        attempt_id: str,
        attempts_made: int,
        error_class: str,
        error_message: str,
        is_retryable: bool,
        had_partial_response: bool,
        usage: dict[str, int | None] | None = None,
    ) -> None:
        self.attempt_id = attempt_id
        self.attempts_made = attempts_made
        self.error_class = error_class
        self.error_message = error_message
        self.is_retryable = is_retryable
        self.had_partial_response = had_partial_response
        self.usage = usage
        reason = "partial_response" if had_partial_response else (
            "retry_exhausted" if is_retryable else "non_retryable"
        )
        super().__init__(
            f"{error_class}（逻辑调用 {attempt_id}，已尝试 {attempts_made} 次，{reason}）：{error_message}"
        )


@dataclass
class AttemptOutcome:
    """单次传输尝试的结构化结果，供调用方写 Trace attempt 事件。"""

    attempt_id: str
    attempt_number: int
    started_at: float
    ended_at: float
    success: bool
    error_class: str | None = None
    error_message: str | None = None
    is_retryable: bool = False
    had_partial_response: bool = False
    response: Any | None = None
    usage: dict[str, int | None] | None = None


@dataclass
class RetryBudget:
    """一次逻辑模型调用的权威重试预算（CON-003：显式、可计算、不依赖 SDK 默认）。"""

    max_attempts: int = MAX_TRANSMIT_ATTEMPTS
    backoff_seconds: float = 1.0
    on_attempt_complete: Callable[[AttemptOutcome], None] | None = field(default=None, repr=False)
    on_backoff: Callable[[str, int, float], None] | None = field(default=None, repr=False)
    # 注入可观测时钟/退避，便于虚拟时钟故障注入测试（AC-005/AC-008）。
    sleep: Callable[[float], None] | None = field(default=None, repr=False)
    sleep_async: Callable[[float], Awaitable[None]] | None = field(default=None, repr=False)


def classify_error(exc: BaseException) -> tuple[str, bool]:
    """把异常归入错误分类，返回 (分类标签, 是否可重试)。

    可重试（DEC-006）：连接失败、连续无响应超时、限流、5xx——且仅当尚未产生部分响应。
    不可重试：认证/权限/参数/内容策略/取消，以及任何"已收到增量后中断"的部分响应。
    取消（asyncio.CancelledError / KeyboardInterrupt）不进重试，直接向上抛。
    """
    name = type(exc).__name__
    # 不可恢复的控制流：取消绝不当成可重试错误（EDGE-004）。
    import asyncio

    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return "cancel", False
    if type(exc).__name__ == "GraphInterrupt":
        return "interrupt", False

    # openai 异常分类（按类名匹配，避免硬 import 依赖供应商版本漂移）。
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return "auth", False
    if name == "BadRequestError":
        return "bad_request", False
    # 内容策略 / 安全拦截：4xx 类，不可重试。
    if name in {"ContentPolicyViolationError", "UnprocessableEntityError"}:
        return "content_policy", False
    # 短暂依赖故障：可重试。
    if name in {"APIConnectionError", "APITimeoutError"}:
        return ("no_response", True) if name == "APITimeoutError" else ("connection", True)
    if name == "RateLimitError":
        return "rate_limited", True
    if name == "APIStatusError":
        status = _extract_status(exc)
        if status is not None and 500 <= status < 600:
            return "server_error", True
        return "http_status", False
    # 兜底：未知异常按不可重试处理（保守，不放大未知风险）。
    return "unknown", False


def _extract_status(exc: BaseException) -> int | None:
    """从 APIStatusError 提取 HTTP 状态码（兼容 openai 版本差异）。"""
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _had_partial_response(exc: BaseException) -> bool:
    """判断调用是否已收到任意响应增量后中断（EDGE-005/DEC-006）。

    部分响应的判据：流式过程中已产出过内容/usage/tool_call，再发生中断。这类调用
    不可整次重放（重复输出、重复副作用、usage 遗漏/重复计费）。openai 的流式部分
    错误通常携带 ``.response`` 或被上层标记；这里按可观察字段判定，缺证据时为 False。
    """
    # 流式上下文中的标记（由模型中间件在收到首块后写入 exc，见 wrapping 处）。
    if getattr(exc, "_writer_partial_response", False):
        return True
    # 携带 usage 增量同样视为已部分到达。
    if getattr(exc, "_writer_seen_usage", None):
        return True
    return False


class WriterRetryController:
    """把一次逻辑模型调用收敛到单一重试预算的包装器。

    用法：执行端用 ``controller.call(...)`` / ``controller.acall(...)`` 替代裸模型调用，
    预算、错误分类与 attempt 可观测全部在此完成。控制器本身不记录 Trace——它通过
    ``budget`` 的回调把结构化 ``AttemptOutcome`` 交给调用方（模型中间件）去写 Trace，
    保持"重试归重试、记录归记录"的单一职责。
    """

    def __init__(self, budget: RetryBudget | None = None) -> None:
        self.budget = budget or RetryBudget()

    def call(self, invoke: Callable[[], Any], *, is_partial: Callable[[BaseException], bool] | None = None) -> Any:
        """同步执行带预算的模型调用。``invoke`` 应执行一次底层传输。"""
        return self._run(invoke, is_partial, sleep=self.budget.sleep)

    async def acall(self, invoke: Callable[[], Awaitable[Any]], *, is_partial: Callable[[BaseException], bool] | None = None) -> Any:
        """异步执行带预算的模型调用。"""
        return await self._run_async(invoke, is_partial)

    # ------------------------------------------------------------------
    # 内部：预算执行
    # ------------------------------------------------------------------

    def _run(
        self,
        invoke: Callable[[], Any],
        is_partial: Callable[[BaseException], bool] | None,
        *,
        sleep: Callable[[float], None] | None,
    ) -> Any:
        attempt_id = uuid.uuid4().hex[:12]
        last_outcome: AttemptOutcome | None = None
        for attempt in range(1, self.budget.max_attempts + 1):
            started = time.perf_counter()
            try:
                response = invoke()
            except BaseException as exc:  # noqa: BLE001 —— 需要捕获所有传输异常以分类
                # 分类只算一次，复用给取消判定与 outcome 记录（避免每失败重复 classify）。
                category, retryable = classify_error(exc)
                # 不可恢复的控制流（取消/中断）：原样向上抛，绝不吞成结构化重试错误（EDGE-004）。
                if category in {"cancel", "interrupt"}:
                    raise
                outcome = self._record_failure(attempt_id, attempt, started, exc, is_partial, category, retryable)
                last_outcome = outcome
                self._notify(outcome)
                # 不可重试 / 已部分响应：立即返回结构化失败，不消耗下一次 attempt。
                if not outcome.is_retryable or outcome.had_partial_response:
                    raise self._to_error(outcome) from exc
                # 还剩预算才退避；否则进入预算用尽。
                if attempt >= self.budget.max_attempts:
                    break
                self._do_backoff(attempt_id, attempt, sleep)
                continue
            outcome = AttemptOutcome(
                attempt_id=attempt_id,
                attempt_number=attempt,
                started_at=started,
                ended_at=time.perf_counter(),
                success=True,
                response=response,
                usage=_maybe_usage(response),
            )
            self._notify(outcome)
            return response

        # 预算用尽（两次连续无响应）：向上返回结构化失败。
        assert last_outcome is not None
        raise self._to_error(last_outcome)

    async def _run_async(
        self,
        invoke: Callable[[], Awaitable[Any]],
        is_partial: Callable[[BaseException], bool] | None,
    ) -> Any:
        attempt_id = uuid.uuid4().hex[:12]
        last_outcome: AttemptOutcome | None = None
        for attempt in range(1, self.budget.max_attempts + 1):
            started = time.perf_counter()
            try:
                response = await invoke()
            except BaseException as exc:  # noqa: BLE001
                category, retryable = classify_error(exc)
                # 不可恢复的控制流（取消/中断）：原样向上抛，绝不吞成结构化重试错误（EDGE-004）。
                if category in {"cancel", "interrupt"}:
                    raise
                outcome = self._record_failure(attempt_id, attempt, started, exc, is_partial, category, retryable)
                last_outcome = outcome
                self._notify(outcome)
                if not outcome.is_retryable or outcome.had_partial_response:
                    raise self._to_error(outcome) from exc
                if attempt >= self.budget.max_attempts:
                    break
                await self._do_backoff_async(attempt_id, attempt)
                continue
            outcome = AttemptOutcome(
                attempt_id=attempt_id,
                attempt_number=attempt,
                started_at=started,
                ended_at=time.perf_counter(),
                success=True,
                response=response,
                usage=_maybe_usage(response),
            )
            self._notify(outcome)
            return response
        assert last_outcome is not None
        raise self._to_error(last_outcome)

    def _record_failure(
        self,
        attempt_id: str,
        attempt: int,
        started: float,
        exc: BaseException,
        is_partial: Callable[[BaseException], bool] | None,
        category: str | None = None,
        retryable: bool | None = None,
    ) -> AttemptOutcome:
        # 复用调用方已算好的分类（None 时回退到重新分类，保留内部 API）。
        if category is None or retryable is None:
            category, retryable = classify_error(exc)
        partial = _had_partial_response(exc) or (is_partial(exc) if is_partial else False)
        # 一旦出现部分响应，重试分类立即关闭——DEC-006 硬约束。
        if partial:
            retryable = False
        return AttemptOutcome(
            attempt_id=attempt_id,
            attempt_number=attempt,
            started_at=started,
            ended_at=time.perf_counter(),
            success=False,
            error_class=category,
            error_message=f"{type(exc).__name__}: {exc}",
            is_retryable=retryable,
            had_partial_response=partial,
        )

    def _notify(self, outcome: AttemptOutcome) -> None:
        if self.budget.on_attempt_complete is not None:
            try:
                self.budget.on_attempt_complete(outcome)
            except Exception:  # noqa: BLE001 —— 观测回调失败不得影响重试主流程
                logger.debug("on_attempt_complete 回调失败", exc_info=True)

    def _do_backoff(self, attempt_id: str, attempt: int, sleep: Callable[[float], None] | None) -> None:
        delay = self.budget.backoff_seconds
        if self.budget.on_backoff is not None:
            try:
                self.budget.on_backoff(attempt_id, attempt, delay)
            except Exception:  # noqa: BLE001
                logger.debug("on_backoff 回调失败", exc_info=True)
        # 默认退避用真实 time.sleep；测试可注入虚拟时钟的 no-op。
        if sleep is not None:
            sleep(delay)
        else:
            import time as _time

            _time.sleep(delay)

    async def _do_backoff_async(self, attempt_id: str, attempt: int) -> None:
        delay = self.budget.backoff_seconds
        if self.budget.on_backoff is not None:
            try:
                self.budget.on_backoff(attempt_id, attempt, delay)
            except Exception:  # noqa: BLE001
                logger.debug("on_backoff 回调失败", exc_info=True)
        if self.budget.sleep_async is not None:
            await self.budget.sleep_async(delay)
        else:
            import asyncio

            await asyncio.sleep(delay)

    def _to_error(self, outcome: AttemptOutcome) -> WriterRetryError:
        return WriterRetryError(
            attempt_id=outcome.attempt_id,
            attempts_made=outcome.attempt_number,
            error_class=outcome.error_class or "unknown",
            error_message=outcome.error_message or "",
            is_retryable=outcome.is_retryable,
            had_partial_response=outcome.had_partial_response,
            usage=outcome.usage,
        )


def _maybe_usage(response: Any) -> dict[str, int | None] | None:
    """从成功响应提取 usage（供计费幂等键使用，缺失返回 None）。"""
    # 复用 trace_middleware 的归一化逻辑，避免重复实现。
    try:
        from app.platform.agent.middleware.trace_middleware import _usage_payload

        return _usage_payload(response)
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "MAX_TRANSMIT_ATTEMPTS",
    "WriterRetryError",
    "AttemptOutcome",
    "RetryBudget",
    "WriterRetryController",
    "classify_error",
]
