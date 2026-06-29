"""进化护栏 middleware。

两个护栏：
  1. EvolutionGuardMiddleware（旧，一把手模式用）：report 前检查两个分数，
     Agent 结束前没产出 report 则注入提示。向后兼容保留。
  2. PhaseGuardMiddleware（新，驱动器模式用）：6 阶段状态机 +
     wrap_tool_call 白名单拦截，强制驱动器按序委托。决策 D-guard/E22。

设计依据：设计文档 D-guard（阶段状态机 + wrap_tool_call 白名单）/ E22。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage

logger = logging.getLogger("evolution.evolve.guard")

# 最多允许注入"请继续到 report"的次数，防死循环
_MAX_NUDGES = 3


# ── 6 阶段定义 + 各阶段工具白名单（D-guard）─────────────────────

# 阶段顺序（D4）
PHASES = [
    "eval_baseline",    # ① 评估 baseline
    "plan",             # ② 方案设计
    "execute",          # ③ 执行落地
    "run_candidate",    # ④ 跑 candidate 测试
    "eval_candidate",   # ⑤ 评估 candidate
    "report",           # ⑥ 出报告
]

# 各阶段允许的工具/委托目标白名单。
# task 工具的 args.subagent_type 决定委托给哪个子代理。
# run_test 的 args.config_variant 区分 baseline/candidate。
PHASE_WHITELIST: dict[str, dict[str, Any]] = {
    "eval_baseline": {
        "tools": {"task"},
        "task_targets": {"evaluate"},
        "desc": "阶段①：委托 evaluate 子代理评估 baseline trace",
    },
    "plan": {
        "tools": {"task"},
        "task_targets": {"plan"},
        "desc": "阶段②：委托 plan 子代理设计改进方案",
    },
    "execute": {
        "tools": {"task"},
        "task_targets": {"execute"},
        "desc": "阶段③：委托 execute 子代理落地改动",
    },
    "run_candidate": {
        "tools": {"run_test"},
        "run_test_variants": {"candidate"},
        "desc": "阶段④：跑 candidate 测试",
    },
    "eval_candidate": {
        "tools": {"task"},
        "task_targets": {"evaluate"},
        "desc": "阶段⑤：委托 evaluate 子代理评估 candidate trace",
    },
    "report": {
        "tools": {"report"},
        "desc": "阶段⑥：产出对比报告",
    },
}


class PhaseGuardMiddleware(AgentMiddleware):
    """驱动器 6 阶段状态机护栏（D-guard/E22）。

    机制：
      - 维护 current_phase（初始 eval_baseline，存 EvolveContext）。
      - wrap_tool_call 拦截驱动器的工具调用，检查是否符合当前阶段白名单。
        不符合 → 返回拒绝 ToolMessage，不执行工具。
      - 工具成功执行后，推进到下一阶段（after 工具回调）。
      - after_agent：未到 report 阶段就结束 → 注入提示继续。

    与一把手 EvolutionGuardMiddleware 的区别：
      - 旧：检查 report 前有两个分数（内容驱动）。
      - 新：6 阶段状态机（流程驱动），白名单精确拦截越权。
    """

    def __init__(self) -> None:
        self._nudge_count = 0

    def _get_phase(self) -> str:
        """从 EvolveContext 取当前阶段。"""
        from app.evolve.tools import get_tool_context
        ctx = get_tool_context()
        if ctx is None:
            return "eval_baseline"
        return ctx.current_phase or "eval_baseline"

    def _set_phase(self, phase: str) -> None:
        from app.evolve.tools import get_tool_context
        ctx = get_tool_context()
        if ctx is None:
            return
        ctx.current_phase = phase
        from app.evolve import db as ev_db
        ev_db.update_session(ctx.session_id, phase=phase)
        ctx.emit_step("phase", phase, phase=phase)

    def _next_phase(self, current: str) -> str | None:
        """推进到下一阶段。已是最后阶段返回 None。"""
        idx = PHASES.index(current) if current in PHASES else -1
        if idx + 1 < len(PHASES):
            return PHASES[idx + 1]
        return None

    def wrap_tool_call(self, request, handler):
        """拦截驱动器的工具调用，检查是否符合当前阶段白名单（D-guard 核心）。

        不符合白名单 → 返回拒绝 ToolMessage（工具不执行）+ 提示。
        符合 → 放行执行 handler，成功后推进阶段。
        """
        from langchain_core.messages import ToolMessage
        from app.evolve.tools import get_tool_context

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

        # 工具执行成功（非拒绝消息）后推进阶段
        # 判断成功：ToolMessage 的 content 不以拦截/错误开头
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
        # run_test 工具：检查 variant
        if tool_name == "run_test":
            variant = tool_args.get("config_variant", "")
            allowed_variants = whitelist.get("run_test_variants", set())
            if variant not in allowed_variants:
                return (
                    f"run_test variant {variant!r} 不在当前阶段允许的 "
                    f"{sorted(allowed_variants)} 内（baseline 不需跑，已有）"
                )
        return None

    def _is_success(self, result: Any) -> bool:
        """判断工具执行是否成功（用于决定是否推进阶段）。"""
        # ToolMessage 的 content 不含拦截/失败标记
        if hasattr(result, "content"):
            content = str(result.content)
            if content.startswith("[PhaseGuard 拦截]"):
                return False
            if "失败" in content[:20] or "错误" in content[:20]:
                return False
            return True
        return True

    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """驱动器想结束时，检查是否到 report 阶段。没到则注入提示继续。"""
        from app.evolve.tools import get_tool_context
        ctx = get_tool_context()
        if ctx is None:
            return None

        phase = self._get_phase()
        if phase == "report" and ctx.report:
            return None  # 已完成

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


# ── 旧护栏（一把手模式，向后兼容保留）──────────────────────────


class EvolutionGuardMiddleware(AgentMiddleware):
    """进化闭环护栏：确保 Agent 走完完整流程才结束。

    在 before_agent 注入「必须完成清单」，在 after_agent 检查是否产出 report。
    若没产出 report 且未超次数，注入提示让 Agent 继续。
    """

    def __init__(self) -> None:
        self._nudge_count = 0

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """流程开始时注入必须完成清单。"""
        return {
            "messages": [
                SystemMessage(content=(
                    "你必须完成完整闭环：run_baseline → read_trace → read_surface → "
                    "分析 → 产出改动 → run_candidate → read_verifier(baseline) → "
                    "read_verifier(candidate) → report。"
                    "不允许在产出 report 之前结束。"
                ))
            ]
        }

    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Agent 想结束时，检查是否已产出 report。

        通过检查当前 session 的 ctx.report 是否已填充判断（D15：改用 contextvar）。
        没产出且未超次数 → 注入提示继续。
        """
        from app.evolve.tools import get_tool_context

        ctx = get_tool_context()
        if ctx is None:
            return None

        # 已产出 report，放行
        if ctx.report:
            return None

        # 未产出，提示继续
        if self._nudge_count >= _MAX_NUDGES:
            logger.warning(
                "session %s: Agent 未产出 report 且已达 nudge 上限，放行结束",
                ctx.session_id,
            )
            return None

        self._nudge_count += 1
        logger.info(
            "session %s: Agent 想结束但未产出 report，注入提示（第 %d 次）",
            ctx.session_id, self._nudge_count,
        )

        missing = []
        if not ctx.baseline_trace:
            missing.append("run_baseline（跑 baseline）")
        if not ctx.candidate_trace:
            missing.append("run_candidate（跑 candidate）")
        if ctx.baseline_score is None:
            missing.append("read_verifier(baseline)（给 baseline 打分）")
        if ctx.candidate_score is None:
            missing.append("read_verifier(candidate)（给 candidate 打分）")

        hint = "你还差这些步骤没完成：" + "、".join(missing) if missing else "你还没产出 report"
        return {
            "messages": [
                SystemMessage(content=(
                    f"{hint}。请继续完成剩余步骤并最终调用 report 工具，"
                    "不要现在结束。"
                ))
            ]
        }


__all__ = ["EvolutionGuardMiddleware", "PhaseGuardMiddleware", "PHASES", "PHASE_WHITELIST"]
