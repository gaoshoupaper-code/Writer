"""WriteResultInspectorMiddleware — 写入结果检查中间件（D3）。

职责：
  在 wrap_tool_call hook 上拦截 write_file / edit_file 的返回值，检测
  status="error" 的 ToolMessage（deepagents FilesystemBackend 把 OSError、
  UnicodeEncodeError、文件已存在等失败都吞进 ToolMessage 正常返回，不抛异常），
  将其转抛为 WriteFailedError，让外层 ErrorRecoveryMiddleware 能 catch 到
  并走重试/恢复链路。

背景（来自 A2 根因分析）：
  生产链路的 ErrorRecoveryMiddleware 只 catch 抛出的异常，对正常返回的 error
  ToolMessage 完全无感——磁盘满、权限拒绝、文件占用、文件已存在这些本应触发
  重试/恢复的场景，被直接喂给 LLM 让它自己琢磨。本中间件填补这个断链。

装配约束：
  必须装在 ErrorRecoveryMiddleware 之内（让 ErrorRecovery 在更外层 catch 抛出
  的 WriteFailedError），同时装在 FileWriteSerializeMiddleware 之内（同一文件
  串行化锁之内，避免异常打断其他并发的锁等待——R2 风险）。

设计依据：.claude/md/20260720_150000_trace交付物丢失与基础设施归因.md §D3
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# 需要检查返回值的写入工具
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})


class WriteFailedError(Exception):
    """写入工具返回 error 状态，转抛让 ErrorRecovery 能 catch。

    设计为可重试异常（不进 ErrorRecovery 的不可恢复列表）——磁盘瞬时忙、
    文件瞬时占用这类瞬时错误确实值得重试。
    """


class WriteResultInspectorMiddleware(AgentMiddleware):
    """写入结果检查中间件。

    拦 write_file / edit_file 的返回值，检测 status="error" 的 ToolMessage，
    转抛 WriteFailedError。其他工具调用完全透传。
    """

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """同步：检查写入工具返回值。"""
        if not self._is_write_tool(request):
            return handler(request)
        result = handler(request)
        self._inspect_result(request, result)
        return result

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """异步：检查写入工具返回值。"""
        if not self._is_write_tool(request):
            return await handler(request)
        result = await handler(request)
        self._inspect_result(request, result)
        return result

    # ------------------------------------------------------------------
    # 内部逻辑
    # ------------------------------------------------------------------

    def _is_write_tool(self, request: Any) -> bool:
        """判断是否为写入工具（write_file / edit_file）。"""
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        return str(tool_name) in _WRITE_TOOLS

    def _inspect_result(self, request: Any, result: Any) -> None:
        """检查返回值：若为 error ToolMessage 则转抛 WriteFailedError。

        只对 ToolMessage 且 status == "error" 触发；其他结果（正常 ToolMessage、
        字符串、其他类型）完全透传，不误报。
        """
        if not isinstance(result, ToolMessage):
            return
        if getattr(result, "status", None) != "error":
            return

        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name") or "write_tool"
        content = result.content if isinstance(result.content, str) else str(result.content)

        logger.warning(
            "WriteResultInspector: %s 返回 error，转抛 WriteFailedError 让 "
            "ErrorRecovery 重试。错误内容：%s",
            tool_name,
            content[:200],
        )
        raise WriteFailedError(f"{tool_name} 失败：{content}")


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


__all__ = ["WriteResultInspectorMiddleware", "WriteFailedError"]
