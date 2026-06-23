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
    """

    def __init__(self, *, max_retries: int = 2, retry_delay: float = 0.5) -> None:
        """
        Args:
            max_retries: 最大重试次数（不含首次调用），默认 2 次
            retry_delay: 重试基础延迟（秒），异步模式下按 attempt+1 递增
        """
        self.max_retries = max_retries
        self.retry_delay = retry_delay

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
        # 所有重试都失败，返回包含恢复建议的错误消息
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
                # 非最后一次重试前等待，延迟按尝试次数递增（0.5s → 1.0s → 1.5s ...）
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
        return self._tool_error_message(request, last_exc)

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
