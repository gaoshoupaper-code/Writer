"""进化 Agent 的闭环护栏 middleware。

核心护栏已在 report 工具内部实现（report 前检查两个分数）。
本 middleware 做「收尾保障」：Agent 结束前如果没产出 report，
注入提示要求它继续直到产出报告。

这是 after_agent hook——在 Agent 决定结束时拦截。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage

logger = logging.getLogger("evolution.evolve.guard")

# 最多允许注入"请继续到 report"的次数，防死循环
_MAX_NUDGES = 3


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

        通过检查 tools.py 的 ctx_global.report 是否已填充判断。
        没产出且未超次数 → 注入提示继续。
        """
        from app.evolve.tools import ctx_global

        if ctx_global is None:
            return None

        # 已产出 report，放行
        if ctx_global.report:
            return None

        # 未产出，提示继续
        if self._nudge_count >= _MAX_NUDGES:
            logger.warning(
                "session %s: Agent 未产出 report 且已达 nudge 上限，放行结束",
                ctx_global.session_id,
            )
            return None

        self._nudge_count += 1
        logger.info(
            "session %s: Agent 想结束但未产出 report，注入提示（第 %d 次）",
            ctx_global.session_id, self._nudge_count,
        )

        missing = []
        if not ctx_global.baseline_trace:
            missing.append("run_baseline（跑 baseline）")
        if not ctx_global.candidate_trace:
            missing.append("run_candidate（跑 candidate）")
        if ctx_global.baseline_score is None:
            missing.append("read_verifier(baseline)（给 baseline 打分）")
        if ctx_global.candidate_score is None:
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


__all__ = ["EvolutionGuardMiddleware"]
