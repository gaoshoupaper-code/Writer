"""RetryMiddleware — LLM 调用指数退避重试中间件。

职责：
  在 wrap_model_call hook 上包裹 LLM 调用，对 APIConnectionError 等瞬态
  网络故障实现指数退避重试。解决 trace 因连续 APIConnectionError 直接失败的问题。

使用方式：
  装配到 agent 的 wrap_model_call hook 处理器列表。
  max_retries: 最大重试次数（默认 3）
  base_delay: 初始退避秒数（默认 2.0）
  max_delay: 最大退避秒数（默认 60.0）
  retryable_exceptions: 可重试的异常类名列表
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

logger = logging.getLogger(__name__)

# 默认可重试异常类名
_DEFAULT_RETRYABLE_EXCEPTIONS = [
    "APIConnectionError",
    "RateLimitError",
    "ServiceUnavailableError",
]


class RetryMiddleware(AgentMiddleware):
    """LLM 调用指数退避重试中间件。

    在 wrap_model_call hook 上包裹模型调用，捕获可重试异常后按指数退避重试。
    重试耗尽后向上抛出原始异常。
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
        retryable_exceptions: list[str] | None = None,
    ) -> None:
        """
        Args:
            max_retries: 最大重试次数（不含首次调用），默认 3
            base_delay: 初始退避秒数，默认 2.0
            max_delay: 最大退避秒数，默认 60.0
            retryable_exceptions: 可重试的异常类名列表，默认包含常见 API 错误
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retryable_exceptions = set(retryable_exceptions or _DEFAULT_RETRYABLE_EXCEPTIONS)

    # ------------------------------------------------------------------
    # wrap_model_call hook：包裹 LLM 调用，重试可恢复错误
    # ------------------------------------------------------------------

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """同步：包裹模型调用，失败时按指数退避重试。"""
        last_exc: BaseException | None = None

        for attempt in range(1 + self.max_retries):
            try:
                return handler(request)
            except BaseException as exc:
                if not self._is_retryable(exc):
                    raise
                last_exc = exc
                if attempt < self.max_retries:
                    delay = min(
                        self.base_delay * (2.0 ** attempt) + random.uniform(0, 0.5),
                        self.max_delay,
                    )
                    logger.warning(
                        "LLM call failed (attempt %d/%d): %s: %s. "
                        "Retrying in %.1fs...",
                        attempt + 1, 1 + self.max_retries,
                        type(exc).__name__, exc,
                        delay,
                    )
                    time.sleep(delay)

        # 所有重试耗尽，抛出原始异常
        logger.error(
            "LLM call failed after %d retries: %s: %s",
            self.max_retries, type(last_exc).__name__, last_exc,
        )
        raise last_exc  # type: ignore[misc]

    async def awrap_model_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """异步：包裹模型调用，失败时按指数退避重试。"""
        last_exc: BaseException | None = None

        for attempt in range(1 + self.max_retries):
            try:
                return await handler(request)
            except BaseException as exc:
                if not self._is_retryable(exc):
                    raise
                last_exc = exc
                if attempt < self.max_retries:
                    delay = min(
                        self.base_delay * (2.0 ** attempt) + random.uniform(0, 0.5),
                        self.max_delay,
                    )
                    logger.warning(
                        "LLM call failed (attempt %d/%d): %s: %s. "
                        "Retrying in %.1fs...",
                        attempt + 1, 1 + self.max_retries,
                        type(exc).__name__, exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

        # 所有重试耗尽，抛出原始异常
        logger.error(
            "LLM call failed after %d retries: %s: %s",
            self.max_retries, type(last_exc).__name__, last_exc,
        )
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _is_retryable(self, exc: BaseException) -> bool:
        """判断异常是否可重试。

        检查异常类名是否在 retryable_exceptions 集合中。
        也检查异常链中的 cause。
        """
        if type(exc).__name__ in self.retryable_exceptions:
            return True
        # 检查异常链
        cause = getattr(exc, "__cause__", None)
        if cause is not None and type(cause).__name__ in self.retryable_exceptions:
            return True
        return False


__all__ = ["RetryMiddleware"]
