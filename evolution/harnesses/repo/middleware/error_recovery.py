"""ErrorRecoveryMiddleware — 工具调用错误恢复中间件。

职责：
  当代理的工具调用抛出异常时，自动重试指定次数。
  重试耗尽后，将错误信息和恢复建议注入对话，让模型自行修正参数或方式。

恢复策略：
  1. 捕获工具调用异常（排除不可恢复的系统级异常）
  2. 按配置的次数重试（默认 2 次）
  3. 重试间隔按尝试次数递增（异步模式：0.5s × (attempt + 1)）
  4. 耗尽后返回包含错误详情和恢复建议的 ToolMessage
  5. 模型根据建议调整参数后可以重新调用工具

不可恢复的异常（直接向上抛出，不重试）：
  - asyncio.CancelledError  — 任务被取消
  - KeyboardInterrupt       — 用户中断
  - SystemExit              — 系统退出

使用方式：
  在构建代理时加入中间件列表。
  max_retries: 最大重试次数（默认 2，总共最多执行 3 次）
  retry_delay: 重试基础延迟（默认 0.5 秒，异步模式下按 attempt 线性递增）
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage


class ErrorRecoveryMiddleware(AgentMiddleware):
    """工具调用错误恢复中间件。

    通过 DeepAgents 的 AgentMiddleware 接口拦截工具调用，
    在调用失败时自动重试，耗尽后注入错误恢复建议。

    task 防重放（CON-005/FR-003/DEC-003）：``task`` 工具运行整个子 Agent，重放会重复
    副作用与计费并制造约 18 分钟乘法等待。注入的 ``tool_replay_policy`` 在重试前判定；
    命中 task 时恒为"不可重试"，错误交回 Meta Agent 用新逻辑任务身份显式重新委派。
    """

    def __init__(
        self,
        *,
        max_retries: int = 2,
        retry_delay: float = 0.5,
        intervention_callback: Callable[..., None] | None = None,
        tool_replay_policy: Any | None = None,
    ) -> None:
        """
        Args:
            max_retries: 最大重试次数（不含首次调用），默认 2 次
            retry_delay: 重试基础延迟（秒），异步模式下按 attempt+1 递增
            tool_replay_policy: 平台注入的 task 防重放策略（CON-005）。非 None 时，
                每次重试前调用 should_retry(tool_name, exc)；返回 False 的工具不重试。
        """
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.intervention_callback = intervention_callback
        self.tool_replay_policy = tool_replay_policy

    def _can_retry(self, request: Any, exc: BaseException) -> bool:
        """平台 task 防重放判定：task 工具不得被通用恢复重放（CON-005）。"""
        if self.tool_replay_policy is None:
            return True
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        return bool(self.tool_replay_policy.should_retry(tool_name, exc))

    # ------------------------------------------------------------------
    # 工具调用拦截（同步 / 异步）
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """拦截同步工具调用：重试 → 耗尽后注入恢复建议。"""
        last_exc: BaseException | None = None
        # 1 + max_retries = 总共执行的次数
        for attempt in range(1 + self.max_retries):
            try:
                return handler(request)
            except BaseException as exc:
                # 不可恢复的异常直接向上抛出
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)) or type(exc).__name__ == "GraphInterrupt":
                    raise
                last_exc = exc
                # task 防重放：命中不可重试工具立即短路，交回 Meta Agent（CON-005）。
                if attempt < self.max_retries and self._can_retry(request, exc):
                    self._emit_intervention("retry", exc)
                    continue
                break
        # 所有重试都失败（或被防重放短路），返回包含恢复建议的错误消息
        self._emit_intervention("short_circuit", last_exc)
        return self._tool_error_message(request, last_exc)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """拦截异步工具调用：重试（带延迟）→ 耗尽后注入恢复建议。"""
        last_exc: BaseException | None = None
        for attempt in range(1 + self.max_retries):
            try:
                return await handler(request)
            except BaseException as exc:
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)) or type(exc).__name__ == "GraphInterrupt":
                    raise
                last_exc = exc
                # task 防重放：命中不可重试工具立即短路，交回 Meta Agent（CON-005）。
                if attempt < self.max_retries and self._can_retry(request, exc):
                    self._emit_intervention("retry", exc)
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                break
        self._emit_intervention("short_circuit", last_exc)
        return self._tool_error_message(request, last_exc)

    def _emit_intervention(self, action: str, exc: BaseException | None) -> None:
        if self.intervention_callback is None:
            return
        try:
            self.intervention_callback(
                action=action,
                hook="wrap_tool_call",
                affected_fields=["control_flow"],
                reason=type(exc).__name__ if exc is not None else None,
            )
        except Exception:
            pass

    def _tool_error_message(self, request: Any, exc: BaseException) -> ToolMessage:
        """构造包含错误详情和恢复建议的工具错误消息。

        消息格式：
          - 重试次数
          - 错误类型和详情
          - 针对错误类型的恢复建议
          - 提示模型调整参数后重试
        """
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        tool_call_id = _mapping_value(tool_call, "id")
        guidance = _recovery_guidance(exc)
        return ToolMessage(
            content=(
                f"工具执行出错（已重试 {self.max_retries} 次）\n\n"
                f"错误类型: {type(exc).__name__}\n"
                f"错误详情: {exc}\n\n"
                f"恢复建议: {guidance}\n\n"
                "请分析错误原因，调整参数或方式后重试。"
            ),
            name=str(tool_name or "unknown"),
            tool_call_id=str(tool_call_id or ""),
            status="error",
        )


# ======================================================================
# 错误类型对应的恢复建议
# ======================================================================


def _recovery_guidance(exc: BaseException) -> str:
    """根据异常类型返回针对性的恢复建议。

    常见文件操作和编码错误的恢复建议：
    - UnicodeEncode/DecodeError → 移除非 UTF-8 字符
    - FileNotFoundError → 检查路径或创建父目录
    - PermissionError → 尝试其他路径
    - IsADirectoryError → 指定文件而非目录路径
    - OSError → 检查磁盘空间或文件锁定
    - JSONDecodeError → 修复 JSON 格式
    - 其他 → 通用建议
    """
    if isinstance(exc, (UnicodeDecodeError, UnicodeEncodeError)):
        return "内容包含非 UTF-8 兼容字符，请移除或替换这些字符后重试。"
    if isinstance(exc, FileNotFoundError):
        return "目标路径不存在，请先创建父目录或检查路径是否正确。"
    if isinstance(exc, PermissionError):
        return "权限不足，无法写入目标路径，请尝试其他路径。"
    if isinstance(exc, IsADirectoryError):
        return "目标路径是一个目录而非文件，请指定完整的文件路径。"
    if isinstance(exc, OSError):
        return "文件系统错误，可能是磁盘空间不足或文件被占用。"
    if isinstance(exc, json.JSONDecodeError):
        return "JSON 格式错误，请检查并修复格式后重试。"
    return "请检查输入参数是否正确，或尝试其他方法完成当前任务。"


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)
