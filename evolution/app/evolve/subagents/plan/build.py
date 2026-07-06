"""方案子代理装配（决策 D11/E16/E14 + S13）。

构建 CompiledSubAgent 并挂载到驱动器。工具来自 plan.tools，
system prompt 来自 plan.prompt。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.trace.recorder import EvolutionTraceRecorder


def build_plan_subagent(
    model,
    *,
    recorder: "EvolutionTraceRecorder | None" = None,
    trace_id_self: str = "",
):
    """构建方案子代理（CompiledSubAgent），挂载到驱动器。

    Args:
        model:         复用驱动器的模型
        recorder:      自观测 trace recorder（进化 ctx 注入）
        trace_id_self: 自观测 trace id（middleware 据此写入事件）
    """
    from deepagents import CompiledSubAgent, create_deep_agent

    from app.evolve.subagents.plan.prompt import PLAN_SYSTEM_PROMPT
    from app.evolve.subagents.plan.tools import make_plan_tools
    from app.trace import TraceMiddleware

    # DeepAgents 的 middleware 不从父 agent 传播到子 agent
    # （SubAgentMiddleware._build_subagent_config 只转发 callbacks/tags/configurable），
    # 故子代理必须各自挂 TraceMiddleware，否则其内部 LLM/工具调用不会被记录。
    middleware_list = []
    if recorder and trace_id_self:
        middleware_list.append(
            TraceMiddleware(recorder=recorder, trace_id=trace_id_self, agent_name="evolve-plan")
        )

    graph = create_deep_agent(
        model=model,
        tools=make_plan_tools(),
        system_prompt=PLAN_SYSTEM_PROMPT,
        middleware=middleware_list,
        subagents=None,
        checkpointer=None,
    )
    return CompiledSubAgent(
        name="plan",
        description=(
            "方案设计专家：读评估报告 + trace，设计具体改进方案（改 prompt/"
            "middleware/参数/源码），产出 design_doc.md。委托时无需额外参数。"
        ),
        runnable=graph,
    )


__all__ = ["build_plan_subagent"]
