"""评估 Agent 专用 middleware：过滤掉框架自带的 filesystem 工具（决策 V4）。

create_deep_agent 强制挂 FilesystemMiddleware（_REQUIRED_MIDDLEWARE），无法移除。
评估 Agent 不需要框架自带的 read_file/write_file/edit_file/glob/grep——
读源码用评估专属的 read_source_file（按版本读，V1），读 trace 用 read_trace*。

本 middleware 在 wrap_model_call 里把这些 filesystem 工具从 request.tools 过滤掉，
确保评估 Agent 只能用评估专属工具，不会误用 read_file 读到当前 working 区
（与按版本对齐的 read_source_file 冲突）。
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
    # execute 也不需要（评估不跑命令）
    "execute",
})


class NoFilesystemToolsMiddleware(AgentMiddleware):
    """过滤掉框架自带的 filesystem 工具，确保评估 Agent 不误用 read_file 等。

    与 PhaseGuard（进化端）不同：这里不是阶段白名单，而是永久移除一批工具。
    机制：wrap_model_call/awrap_model_call 里把 _FILTERED_TOOL_NAMES 里的工具
    从 request.tools 剔除。

    注意：评估 Agent 用 ainvoke 异步跑（agent.py），故必须实现 awrap_model_call，
    否则 langchain 在 async 路径会抛 NotImplementedError（参考 start.log）。
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
