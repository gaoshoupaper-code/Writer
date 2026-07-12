"""进化 Agent 轻量产出约束 middleware（决策 S3/S4）。

单体进化 Agent 没有阶段顺序约束（D3），但需要两条产出依赖约束
防止 Agent 漏产出或乱序产出关键文档：

  1. 无 design_doc 拦 change_log（wrap_tool_call）：
     write_change_log 调用时检查 design_doc_path。审查链路依赖 design_doc，
     没 design_doc 就产 change_log 会导致前端"报告不完整"。

  2. 结束前检查产出齐（after_agent）：
     design_doc + change_log 都产出才允许干净结束。未齐则 nudge（注入提示），
     最多 3 次后放行（防死循环卡死）。

与旧 PhaseGuardMiddleware 的区别：
  - 不维护阶段状态（无 plan/execute phase）
  - 不强制工作流程顺序
  - 只做产出依赖检查（design_doc 必须在 change_log 之前）

注意：Agent 用 ainvoke 异步跑，wrap_tool_call/after_agent 必须成对实现
同步+异步版本，否则 async 路径抛 NotImplementedError。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage

logger = logging.getLogger("evolution.evolve.agent.flow_guard")

# 最多 nudge 次数，防 Agent 反复结束 → 注入 → 结束 死循环
_MAX_NUDGES = 3


class FlowGuardMiddleware(AgentMiddleware):
    """单体进化 Agent 的轻量产出约束（S4）。

    两条约束：
      1. wrap_tool_call：write_change_log 前必须有 design_doc。
      2. after_agent：design_doc + change_log 都齐才放行结束，否则 nudge。
    """

    def __init__(self) -> None:
        self._nudge_count = 0

    # ── 约束 ①：无 design_doc 拦 change_log ──────────────────────

    @staticmethod
    def _check_change_log_guard() -> str | None:
        """检查 write_change_log 前置条件。返回违规描述，None 表示合规。"""
        from app.evolve.ctx import get_tool_context
        ctx = get_tool_context()
        if ctx is None:
            return None  # 无 ctx 不拦（工具自身会报错）
        if not ctx.design_doc_path:
            return (
                "write_change_log 前必须先产出 design_doc（调用 write_design_doc）。"
                "审查链路依赖 design_doc，请先完成方案设计。"
            )
        return None

    def _guard_tool_call(self, request: Any) -> str | None:
        """共享拦截逻辑：检查工具调用是否满足产出依赖。返回违规描述或 None。"""
        tool_name = request.tool_call.get("name", "")
        if tool_name == "write_change_log":
            return self._check_change_log_guard()
        return None

    def wrap_tool_call(self, request: Any, handler: Callable[..., Any]) -> Any:
        """同步路径：拦截 write_change_log（invoke/stream 时走这里）。"""
        violation = self._guard_tool_call(request)
        if violation:
            from langchain_core.messages import ToolMessage
            return ToolMessage(
                content=f"[FlowGuard 拦截] {violation}",
                tool_call_id=request.tool_call.get("id", ""),
                name=request.tool_call.get("name", ""),
            )
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[..., Any]) -> Any:
        """异步路径：拦截 write_change_log（ainvoke/astream 时走这里）。

        与 wrap_tool_call 逻辑一致，仅 handler 改为 await。
        """
        violation = self._guard_tool_call(request)
        if violation:
            from langchain_core.messages import ToolMessage
            return ToolMessage(
                content=f"[FlowGuard 拦截] {violation}",
                tool_call_id=request.tool_call.get("id", ""),
                name=request.tool_call.get("name", ""),
            )
        return await handler(request)

    # ── 约束 ②：结束前检查产出齐 ────────────────────────────────

    def _check_completion(self) -> dict[str, Any] | None:
        """Agent 想结束时，检查 design_doc + change_log 是否都齐。

        都齐 → 返回 None（放行结束）。
        未齐 + nudge 未超限 → nudge_count++，返回注入消息。
        未齐 + nudge 超限 → 返回 None（放行结束，防死循环）。
        """
        from app.evolve.ctx import get_tool_context
        ctx = get_tool_context()

        if ctx is not None and ctx.design_doc_path and ctx.change_log_path:
            return None  # 两个产出都齐，放行

        # 未齐
        if self._nudge_count >= _MAX_NUDGES:
            logger.warning(
                "FlowGuard: 达 nudge 上限（%d），放行结束。design_doc=%s change_log=%s",
                self._nudge_count,
                bool(ctx and ctx.design_doc_path),
                bool(ctx and ctx.change_log_path),
            )
            return None

        self._nudge_count += 1
        missing: list[str] = []
        if not ctx or not ctx.design_doc_path:
            missing.append("design_doc（调用 write_design_doc 产出）")
        if not ctx or not ctx.change_log_path:
            missing.append("change_log（调用 write_change_log 产出）")
        return {
            "messages": [
                SystemMessage(content=(
                    f"进化流程尚未完成——还缺产出：{'、'.join(missing)}。"
                    f"请继续完成后再结束。"
                ))
            ]
        }

    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """同步路径：Agent 结束时检查产出。"""
        return self._check_completion()

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """异步路径：Agent 用 ainvoke 跑时走这里。逻辑与 after_agent 一致。"""
        return self._check_completion()


__all__ = ["FlowGuardMiddleware"]
