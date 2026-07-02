"""方案子代理装配（决策 D11/E16/E14 + S13）。

构建 CompiledSubAgent 并挂载到驱动器。工具来自 plan.tools，
system prompt 来自 plan.prompt。
"""
from __future__ import annotations


def build_plan_subagent(model):
    """构建方案子代理（CompiledSubAgent），挂载到驱动器。"""
    from deepagents import CompiledSubAgent, create_deep_agent

    from app.evolve.subagents.plan.prompt import PLAN_SYSTEM_PROMPT
    from app.evolve.subagents.plan.tools import make_plan_tools

    graph = create_deep_agent(
        model=model,
        tools=make_plan_tools(),
        system_prompt=PLAN_SYSTEM_PROMPT,
        middleware=[],
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
