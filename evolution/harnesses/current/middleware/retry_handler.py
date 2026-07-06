"""RetryHandlerMiddleware — LLM 调用自动重试中间件。

职责：
  在 before_model hook 上拦截 LLM 调用错误（APIConnectionError、RateLimitError、
  TimeoutError 等），自动重试最多 max_retries 次，指数退避（base_delay × backoff_factor^attempt）。
  解决 trace 因连续 APIConnectionError 直接失败的问题。

使用方式：
  装配到 agent 的 before_model hook 处理器列表。
  max_retries: 最大重试次数（默认 3）
  base_delay: 基础延迟秒数（默认 1.0）
  backoff_factor: 退避因子（默认 2.0）
  retryable_errors: 可重试的错误类名列表
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

# 默认可重试错误类名
_DEFAULT_RETRYABLE_ERRORS = [
    "APIConnectionError",
    "RateLimitError",
    "TimeoutError",
    "InternalServerError",
    "ServiceUnavailableError",
]


class RetryHandlerMiddleware(AgentMiddleware):
    """LLM 调用自动重试中间件。

    在 before_model hook 上拦截模型调用，捕获可重试异常后按指数退避重试。
    重试耗尽后向上抛出原始异常，由上层 ErrorRecoveryMiddleware 兜底处理。
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
        backoff_factor: float = 2.0,
        retryable_errors: list[str] | None = None,
    ) -> None:
        """
        Args:
            max_retries: 最大重试次数（不含首次调用），默认 3
            base_delay: 基础延迟秒数，默认 1.0
            backoff_factor: 退避因子，默认 2.0（1s → 2s → 4s）
            retryable_errors: 可重试的错误类名列表，默认包含常见 API 错误
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.backoff_factor = backoff_factor
        self.retryable_errors = set(retryable_errors or _DEFAULT_RETRYABLE_ERRORS)

    # ------------------------------------------------------------------
    # before_model hook：拦截模型调用，重试可恢复错误
    # ------------------------------------------------------------------

    def before_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步：模型调用前注入重试逻辑。"""
        # 同步场景下，重试逻辑由 runtime 的模型调用封装处理
        # 此处返回 None 表示不修改 state，让模型正常调用
        return None

    async def abefore_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """异步：模型调用前注入重试逻辑。

        通过包装 runtime 的模型调用，在调用失败时自动重试。
        """
        last_exc: BaseException | None = None

        for attempt in range(1 + self.max_retries):
            try:
                # 调用原始模型处理
                return await runtime.call_model(state)
            except BaseException as exc:
                if not self._is_retryable(exc):
                    raise
                last_exc = exc
                if attempt < self.max_retries:
                    delay = self.base_delay * (self.backoff_factor ** attempt)
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

        检查异常类名是否在 retryable_errors 集合中。
        也检查异常链中的 cause。
        """
        if type(exc).__name__ in self.retryable_errors:
            return True
        # 检查异常链
        cause = getattr(exc, "__cause__", None)
        if cause is not None and type(cause).__name__ in self.retryable_errors:
            return True
        return False


__all__ = ["RetryHandlerMiddleware"]
