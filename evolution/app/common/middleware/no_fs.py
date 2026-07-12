"""共享中间件：过滤掉框架自带的 filesystem 工具（决策 S7）。

create_deep_agent 强制挂 FilesystemMiddleware（_REQUIRED_MIDDLEWARE），无法移除。
评估 Agent 和进化 Agent 都不需要框架自带的 read_file/write_file/edit_file/glob/grep——
它们用各自的专属工具操作，不走框架裸 fs 工具。

本 middleware 在 wrap_model_call 里把这些 filesystem 工具从 request.tools 过滤掉，
确保 Agent 只能用各自挂载的专属工具。

注意：Agent 用 ainvoke 异步跑，故必须实现 awrap_model_call，
否则 langchain 在 async 路径会抛 NotImplementedError。
"""
from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware

# 要过滤的 filesystem 工具名（FilesystemMiddleware 提供的）
_FILTERED_TOOL_NAMES = frozenset({
    "read_file",
    "write_file",
    "edit_file",
    "ls",
    "glob",
    "grep",
    "execute",
})


class NoFilesystemToolsMiddleware(AgentMiddleware):
    """过滤掉框架自带的 filesystem 工具，确保 Agent 不误用 read_file 等。

    机制：wrap_model_call/awrap_model_call 里把 _FILTERED_TOOL_NAMES 里的工具
    从 request.tools 剔除（用 request.override(tools=...) 重建请求）。

    注意：Agent 用 ainvoke 异步跑，故必须实现 awrap_model_call，
    否则 langchain 在 async 路径会抛 NotImplementedError。
    """

    @staticmethod
    def _filter(request: Any) -> Any:
        """过滤掉 filesystem 工具，返回（可能重建后的）request；逻辑同步/异步共用。"""
        tools = getattr(request, "tools", None)
        if not tools:
            return request

        def _tool_name(t: Any) -> str:
            if hasattr(t, "name"):
                return t.name
            if isinstance(t, dict):
                return t.get("name", "")
            return ""

        filtered = [t for t in tools if _tool_name(t) not in _FILTERED_TOOL_NAMES]
        if len(filtered) == len(tools):
            # 无需过滤（本就没这些工具）
            return request

        # 用 request.override(tools=...) 重建请求（与 FilesystemMiddleware 同模式）
        return request.override(tools=filtered)

    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[..., Any],
    ) -> Any:
        """过滤 filesystem 工具后再调 handler（同步路径）。"""
        return handler(self._filter(request))

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[..., Any],
    ) -> Any:
        """过滤 filesystem 工具后再调 handler（异步路径，ainvoke 时走这里）。"""
        return await handler(self._filter(request))


__all__ = ["NoFilesystemToolsMiddleware"]
