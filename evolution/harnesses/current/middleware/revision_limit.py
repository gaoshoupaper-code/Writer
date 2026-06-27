"""RevisionLimitMiddleware — 修订次数硬上限中间件。

职责：
  拦截 DeepAgent 的 task 工具调用，当目标是 review 子代理时累计调用次数。
  超过配置的最大修订次数后，返回强制终止的 ToolMessage，阻止继续调用 review。

使用方式：
  在构建 DeepAgent 子代理时加入中间件列表。
  max_revisions: 最大修订（review 调用）次数，默认 1。
  review_name: review 子代理的名称，默认 "review"。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage


class RevisionLimitMiddleware(AgentMiddleware):
    """修订次数硬上限中间件。

    通过拦截 task 工具调用，当目标是 review 子代理时累计计数，
    超过上限后返回包含终止指令的 ToolMessage，阻止继续修订。
    """

    def __init__(self, *, max_revisions: int = 1, review_name: str = "review") -> None:
        """
        Args:
            max_revisions: 最大修订次数（即 review 子代理最大被调用次数），默认 1
            review_name: review 子代理的注册名称，用于匹配 task 工具调用的目标
        """
        self.max_revisions = max_revisions
        self.review_name = review_name
        self._revision_count = 0

    # ------------------------------------------------------------------
    # 调用周期重置（子代理每次被 task 调用开始时触发）
    # ------------------------------------------------------------------
    # 计数周期 = 「子代理每被父 agent task 委托一次」。
    # 子代理 graph 一次编译、会话内多次复用同一实例，必须靠 before_agent 在每次
    # graph 执行开始时清零计数，否则额度会跨调用累积（见需求基准计数边界决策）。

    def before_agent(self, state: Any, runtime: Any) -> None:
        self._revision_count = 0

    def abefore_agent(self, state: Any, runtime: Any) -> None:
        self._revision_count = 0

    def _is_review_task(self, request: Any) -> bool:
        """判断 task 工具调用是否目标是 review 子代理。"""
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        if tool_name != "task":
            return False
        args = _mapping_value(tool_call, "args")
        if not isinstance(args, dict):
            return False
        # task 工具的 args 中 subagent_type 指定目标子代理名称
        target = args.get("subagent_type") or args.get("name") or ""
        return target == self.review_name

    def _limit_message(self, request: Any) -> ToolMessage:
        """构造达到审查上限的终止消息：停止审查 + 指示子代理在返回摘要中转述（解读A 可见性）。"""
        tool_call = getattr(request, "tool_call", {})
        tool_call_id = _mapping_value(tool_call, "id")
        return ToolMessage(
            content=(
                f"已达到本轮审查上限（{self.max_revisions} 次 / 本轮创作）。"
                "请接受当前版本，不要再调用 review 审查。"
                "请在返回给父代理的摘要中明确注明：「本轮因达到审查上限，已跳过 review 审查」，"
                "再基于当前内容收尾返回。"
            ),
            name="task",
            tool_call_id=str(tool_call_id or ""),
        )

    # ------------------------------------------------------------------
    # 工具调用拦截（同步 / 异步）
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """拦截同步工具调用：检查修订次数 → 放行或返回终止消息。"""
        if self._is_review_task(request):
            self._revision_count += 1
            if self._revision_count > self.max_revisions:
                return self._limit_message(request)
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """拦截异步工具调用：检查修订次数 → 放行或返回终止消息。"""
        if self._is_review_task(request):
            self._revision_count += 1
            if self._revision_count > self.max_revisions:
                return self._limit_message(request)
        return await handler(request)


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)
