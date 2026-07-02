"""进化护栏 middleware（三功能解耦，决策 S3/S6）。

精简为 2 阶段状态机（plan → execute），删除原 6 阶段白名单
（评估×2、run_candidate、report 已随三功能解耦废弃）。

机制：
  - 维护 current_phase（初始 plan，存 EvolveContext.review_status 旁的阶段标记）
  - wrap_tool_call 拦截驱动器的 task 委托，检查是否符合当前阶段白名单
  - 工具成功后推进阶段
  - after_agent：未到 execute 完成就结束 → 注入提示继续

设计依据：设计文档 S3（2 阶段 guard）/ D-guard（阶段状态机 + 白名单机制保留）。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage

logger = logging.getLogger("evolution.evolve.guard")

# 最多允许注入"请继续"的次数，防死循环
_MAX_NUDGES = 3


# ── 2 阶段定义 + 各阶段工具白名单（S3）──────────────────────────

# 阶段顺序（评估已独立成 Agent，进化只剩方案→执行）
PHASES = [
    "plan",     # ① 方案设计（吃评估报告 + 读 trace，产改进方案）
    "execute",  # ② 执行落地（按方案改源码 + validate_changes）
]

# 各阶段允许的 task 委托目标白名单。
PHASE_WHITELIST: dict[str, dict[str, Any]] = {
    "plan": {
        "tools": {"task"},
        "task_targets": {"plan"},
        "desc": "阶段①：委托 plan 子代理，基于评估报告设计改进方案",
    },
    "execute": {
        "tools": {"task"},
        "task_targets": {"execute"},
        "desc": "阶段②：委托 execute 子代理，按方案落地代码改动 + 校验",
    },
}

# 进化流程结束阶段（无 report 阶段了，execute 完即结束）
TERMINAL_PHASE = "execute"


class PhaseGuardMiddleware(AgentMiddleware):
    """驱动器 2 阶段状态机护栏（S3）。

    机制：
      - 维护 current_phase（初始 plan，存 EvolveContext）。
      - wrap_tool_call 拦截驱动器的工具调用，检查是否符合当前阶段白名单。
        不符合 → 返回拒绝 ToolMessage，不执行工具。
      - 工具成功执行后，推进到下一阶段（after 工具回调）。
      - after_agent：未到 execute 阶段就结束 → 注入提示继续。
    """

    def __init__(self) -> None:
        self._nudge_count = 0
        self._phase: str = PHASES[0]  # plan

    def _get_phase(self) -> str:
        """取当前阶段。"""
        return self._phase

    def _set_phase(self, phase: str) -> None:
        """设置当前阶段 + 通知事件总线。"""
        self._phase = phase
        from app.evolve.ctx import get_tool_context
        ctx = get_tool_context()
        if ctx is not None:
            ctx.emit_step("phase", phase, phase=phase)

    def _next_phase(self, current: str) -> str | None:
        """推进到下一阶段。已是最后阶段返回 None。"""
        idx = PHASES.index(current) if current in PHASES else -1
        if idx + 1 < len(PHASES):
            return PHASES[idx + 1]
        return None

    def wrap_tool_call(self, request, handler):
        """拦截驱动器的工具调用，检查是否符合当前阶段白名单。"""
        from langchain_core.messages import ToolMessage

        tool_name = request.tool_call.get("name", "")
        tool_args = request.tool_call.get("args", {})
        phase = self._get_phase()
        whitelist = PHASE_WHITELIST.get(phase, {})

        # 检查白名单
        violation = self._check_whitelist(tool_name, tool_args, whitelist)
        if violation:
            return ToolMessage(
                content=f"[PhaseGuard 拦截] 当前是{phase}阶段，{violation}。"
                        f"请只做：{whitelist.get('desc', '')}。",
                tool_call_id=request.tool_call.get("id", ""),
                name=tool_name,
            )

        # 放行执行
        result = handler(request)

        # 工具执行成功后推进阶段
        if self._is_success(result):
            nxt = self._next_phase(phase)
            if nxt:
                self._set_phase(nxt)
                logger.info("PhaseGuard: %s → %s", phase, nxt)

        return result

    def _check_whitelist(self, tool_name: str, tool_args: dict, whitelist: dict) -> str | None:
        """检查工具调用是否符合阶段白名单。返回违规描述，None 表示合规。"""
        allowed_tools = whitelist.get("tools", set())
        if tool_name not in allowed_tools:
            return (
                f"工具 {tool_name} 不在当前阶段允许的工具 {sorted(allowed_tools)} 内"
            )
        # task 工具：检查委托目标
        if tool_name == "task":
            target = tool_args.get("subagent_type", "")
            allowed_targets = whitelist.get("task_targets", set())
            if target not in allowed_targets:
                return (
                    f"task 委托目标 {target!r} 不在当前阶段允许的目标 "
                    f"{sorted(allowed_targets)} 内"
                )
        return None

    def _is_success(self, result: Any) -> bool:
        """判断工具执行是否成功（用于决定是否推进阶段）。"""
        if hasattr(result, "content"):
            content = str(result.content)
            if content.startswith("[PhaseGuard 拦截]"):
                return False
            if "失败" in content[:20] or "错误" in content[:20]:
                return False
            return True
        return True

    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """驱动器想结束时，检查是否到 execute 阶段。没到则注入提示继续。"""
        phase = self._get_phase()
        # execute 阶段完成才允许结束
        if phase == TERMINAL_PHASE:
            # 还需确认 execute 已跑过（通过 change_log 是否产出判断）
            from app.evolve.ctx import get_tool_context
            ctx = get_tool_context()
            if ctx is not None and ctx.change_log_path:
                return None  # execute 已产出 change_log，放行结束

        if self._nudge_count >= _MAX_NUDGES:
            logger.warning("PhaseGuard: 达 nudge 上限，放行结束。phase=%s", phase)
            return None

        self._nudge_count += 1
        current_desc = PHASE_WHITELIST.get(phase, {}).get("desc", phase)
        nxt_idx = PHASES.index(phase) if phase in PHASES else 0
        remaining = PHASES[nxt_idx:]
        return {
            "messages": [
                SystemMessage(content=(
                    f"当前在 {phase} 阶段（{current_desc}）。"
                    f"还剩阶段：{' → '.join(remaining)}。"
                    f"请继续完成当前阶段，不要现在结束。"
                ))
            ]
        }


__all__ = ["PhaseGuardMiddleware", "PHASES", "PHASE_WHITELIST", "TERMINAL_PHASE"]
