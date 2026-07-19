"""进化 Agent 阶段门控 + 产出约束 middleware（决策 S3/S4 + T9 阶段门控）。

对话式共创工作台引入两套约束：

  ① 阶段门控（决策 T9，conversing 阶段锁落地工具）：
     conversing 状态下，落地工具（write_* / edit_source / validate_changes /
     write_design_doc / write_change_log）被拦截——Agent 必须先和用户对齐
     进化点（propose → 用户拍板 → finalize）才能进入 finalizing 落地。
     finalizing 状态下解锁全部工具。
     这保证"未拍板不改码"是硬约束，不靠提示词自律。

  ② 产出依赖（决策 S4，沿用原 FlowGuard 逻辑）：
     write_change_log 前必须有 design_doc（审查链路依赖）。
     design_doc + change_log 都齐才放行 Agent 结束（防漏产出）。

阶段判定：从 EvolveContext.session_status 读（conversing/finalizing/running 等）。
单体兼容模式（status=running）下不做阶段门控，沿用原产出依赖逻辑——
现有 run_evolve_session 单体调用不受影响。

注意：Agent 用 ainvoke/astream 异步跑，wrap_tool_call/after_agent 必须成对
实现同步+异步版本。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage

logger = logging.getLogger("evolution.evolve.agent.flow_guard")

# 最多 nudge 次数，防 Agent 反复结束 → 注入 → 结束 死循环
_MAX_NUDGES = 3

# ── 落地工具集（conversing 阶段拦截，决策 T9）──────────────────────
# 这些工具改 harness 源码或产出落地文档，conversing 阶段必须禁止——
# 否则 Agent 可能在用户没拍板前就改了代码。finalizing 阶段解锁。
FINALIZE_ONLY_TOOLS = frozenset({
    # writers.py（改源码）
    "write_prompt",
    "write_middleware",
    "write_tool",
    "write_skill",
    "write_subagent",
    "edit_source",
    # flow.py（产出落地文档）
    "write_design_doc",
    "validate_changes",
    "write_change_log",
})


class FlowGuardMiddleware(AgentMiddleware):
    """进化 Agent 阶段门控 + 产出约束（决策 S4 + T9）。

    约束：
      1. wrap_tool_call：conversing 阶段拦截 FINALIZE_ONLY_TOOLS（阶段门控）。
      2. wrap_tool_call：write_change_log 前必须有 design_doc（产出依赖）。
      3. after_agent：design_doc + change_log 都齐才放行结束（防漏产出）。
    """

    def __init__(self) -> None:
        self._nudge_count = 0

    # ── 阶段门控（决策 T9）──────────────────────────────────────

    @staticmethod
    def _check_phase_guard(tool_name: str) -> str | None:
        """阶段门控：conversing 阶段拦截落地工具。

        Returns:
            违规描述（拦截），None 表示放行。
        """
        from app.evolve.ctx import get_tool_context, STATUS_CONVERSING
        ctx = get_tool_context()
        if ctx is None:
            return None  # 无 ctx 不拦（工具自身会报错）

        # 单体兼容模式（running）和 finalizing 都放行；只 conversing 拦
        if ctx.session_status != STATUS_CONVERSING:
            return None

        if tool_name in FINALIZE_ONLY_TOOLS:
            return (
                f"当前是对话共创阶段（conversing），不能调用 {tool_name}——"
                f"这是落地工具，会修改 harness 源码或产出落地文档。"
                f"请先通过 propose_evolution_point 提出改进点，等用户在对话中拍板"
                f"（accepted 至少一个进化点）后，由用户触发 finalize 进入落地阶段。"
            )
        return None

    # ── 产出依赖（决策 S4，原 FlowGuard 逻辑保留）──────────────────

    @staticmethod
    def _check_change_log_guard() -> str | None:
        """检查 write_change_log 前置条件。返回违规描述，None 表示合规。"""
        from app.evolve.ctx import get_tool_context
        ctx = get_tool_context()
        if ctx is None:
            return None
        if not ctx.design_doc_path:
            return (
                "write_change_log 前必须先产出 design_doc（调用 write_design_doc）。"
                "审查链路依赖 design_doc，请先完成方案设计。"
            )
        return None

    def _guard_tool_call(self, request: Any) -> str | None:
        """共享拦截逻辑：阶段门控 + 产出依赖。返回违规描述或 None。"""
        tool_name = request.tool_call.get("name", "")

        # ① 阶段门控（conversing 拦截落地工具）
        phase_violation = self._check_phase_guard(tool_name)
        if phase_violation:
            return phase_violation

        # ② 产出依赖（change_log 前需 design_doc）
        if tool_name == "write_change_log":
            return self._check_change_log_guard()

        return None

    def wrap_tool_call(self, request: Any, handler: Callable[..., Any]) -> Any:
        """同步路径：拦截违规工具调用（invoke/stream 时走这里）。"""
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
        """异步路径：拦截违规工具调用（ainvoke/astream 时走这里）。"""
        violation = self._guard_tool_call(request)
        if violation:
            from langchain_core.messages import ToolMessage
            return ToolMessage(
                content=f"[FlowGuard 拦截] {violation}",
                tool_call_id=request.tool_call.get("id", ""),
                name=request.tool_call.get("name", ""),
            )
        return await handler(request)

    # ── 结束前检查产出齐 ────────────────────────────────────────

    def _check_completion(self) -> dict[str, Any] | None:
        """Agent 想结束时，检查 design_doc + change_log 是否都齐。

        仅在 finalizing/单体模式下检查（这两种状态期待两个产出都齐）。
        conversing 阶段允许随时结束（等用户下一条消息）。

        都齐 → 返回 None（放行结束）。
        未齐 + nudge 未超限 → nudge_count++，返回注入消息。
        未齐 + nudge 超限 → 返回 None（放行结束，防死循环）。
        """
        from app.evolve.ctx import get_tool_context, STATUS_CONVERSING
        ctx = get_tool_context()

        # conversing 阶段允许 Agent 结束（等用户下一条消息，不强制产出）
        if ctx is not None and ctx.session_status == STATUS_CONVERSING:
            return None

        if ctx is not None and ctx.design_doc_path and ctx.change_log_path:
            return None  # 两个产出都齐，放行

        # 未齐
        if self._nudge_count >= _MAX_NUDGES:
            logger.warning(
                "FlowGuard: 达 nudge 上限（%d），放行结束。design_doc=%s change_log=%s status=%s",
                self._nudge_count,
                bool(ctx and ctx.design_doc_path),
                bool(ctx and ctx.change_log_path),
                ctx.session_status if ctx else None,
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


__all__ = ["FlowGuardMiddleware", "FINALIZE_ONLY_TOOLS"]
