"""RevisionLimitMiddleware — 修订次数硬上限中间件。

职责：
  拦截 DeepAgent 的 task 工具调用，当目标是 evolution 子代理时累计调用次数。
  超过配置的最大修订次数后，返回强制终止的 ToolMessage，阻止继续调用 evolution。

使用方式：
  在构建 DeepAgent 子代理时加入中间件列表。
  max_revisions: 最大修订（evolution 调用）次数，默认 3。
  evolution_name: evolution 子代理的名称，默认 "evolution"。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage


class RevisionLimitMiddleware(AgentMiddleware):
    """修订次数硬上限中间件。

    通过拦截 task 工具调用，当目标是 evolution 子代理时累计计数，
    超过上限后返回包含终止指令的 ToolMessage，阻止继续修订。
    """

    def __init__(self, *, max_revisions: int = 3, evolution_name: str = "evolution") -> None:
        """
        Args:
            max_revisions: 最大修订次数（即 evolution 子代理最大被调用次数），默认 3
            evolution_name: evolution 子代理的注册名称，用于匹配 task 工具调用的目标
        """
        self.max_revisions = max_revisions
        self.evolution_name = evolution_name
        self._revision_count = 0

    def _is_evolution_task(self, request: Any) -> bool:
        """判断 task 工具调用是否目标是 evolution 子代理。"""
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        if tool_name != "task":
            return False
        args = _mapping_value(tool_call, "args")
        if not isinstance(args, dict):
            return False
        # task 工具的 args 中 subagent_type 指定目标子代理名称
        target = args.get("subagent_type") or args.get("name") or ""
        return target == self.evolution_name

    def _limit_message(self, request: Any) -> ToolMessage:
        """构造达到修订上限的终止消息。"""
        tool_call = getattr(request, "tool_call", {})
        tool_call_id = _mapping_value(tool_call, "id")
        return ToolMessage(
            content=(
                f"已达到修订上限（{self.max_revisions} 轮）。"
                "请接受当前版本，不要再调用 evolution 评估。"
                "直接基于当前内容向父代理返回结果即可。"
            ),
            name="task",
            tool_call_id=str(tool_call_id or ""),
        )

    # ------------------------------------------------------------------
    # 工具调用拦截（同步 / 异步）
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """拦截同步工具调用：检查修订次数 → 放行或返回终止消息。"""
        if self._is_evolution_task(request):
            self._revision_count += 1
            if self._revision_count > self.max_revisions:
                return self._limit_message(request)
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """拦截异步工具调用：检查修订次数 → 放行或返回终止消息。"""
        if self._is_evolution_task(request):
            self._revision_count += 1
            if self._revision_count > self.max_revisions:
                return self._limit_message(request)
        return await handler(request)


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)
