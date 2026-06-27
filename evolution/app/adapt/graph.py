"""adapt StateGraph —— AEGIS 进化循环的 graph 骨架（Phase 8，Task 7.2）。

用 LangGraph StateGraph 实现 adaptation loop（决策 A9）。节点 = 阶段，边 = 流转。
固定流程（决策 A10a），无总控 LLM。

graph 结构（设计文档 adapt 第三轮）：

  START
    → run_baseline
    → planner
    → evolver
    → run_candidates
    → evaluate
    → critic ──(revision 且 count<1)──→ evolver（回环，E7a）
    │        ──(pass/reject)──────────→ gate
    → gate ──(shipped)──→ ship
    │       ──(rejected/idle)──→ loop_control
    → ship ──→ loop_control
    → loop_control ──(continue)──→ planner（下一轮）
    │              ──(done)──────→ END

run_baseline 只在 round=0 时执行（后续轮复用基准，E6a 固定基准）。

设计依据：设计文档 A9/A10a/E7a + adapt graph 结构。
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from app.adapt.state import AdaptState

logger = logging.getLogger("evolution.adapt.graph")


def build_adapt_graph():
    """构建 adaptation loop 的 StateGraph。

    节点函数从 app.adapt.nodes 导入。返回编译后的 graph（可 invoke/astream）。

    Returns:
        编译后的 StateGraph（带 checkpointer 则可恢复，决策 E4a）
    """
    # 延迟 import 节点（避免循环依赖：nodes 可能 import graph 的工具）
    from app.adapt.nodes.runner import run_baseline, run_candidates, evaluate
    from app.adapt.nodes.planner import planner
    from app.adapt.nodes.evolver import evolver
    from app.adapt.nodes.critic import critic
    from app.adapt.nodes.gate import gate
    from app.adapt.nodes.ship import ship
    from app.adapt.nodes.loop_control import loop_control

    graph = StateGraph(AdaptState)

    # ── 添加节点 ──
    graph.add_node("run_baseline", run_baseline)
    graph.add_node("planner", planner)
    graph.add_node("evolver", evolver)
    graph.add_node("run_candidates", run_candidates)
    graph.add_node("evaluate", evaluate)
    graph.add_node("critic", critic)
    graph.add_node("gate", gate)
    graph.add_node("ship", ship)
    graph.add_node("loop_control", loop_control)

    # ── 固定边 ──
    # round=0：从 START 跑基准；后续轮直接到 planner（基准不变，E6a）
    graph.add_conditional_edges(
        START,
        _route_start,
        {
            "run_baseline": "run_baseline",
            "planner": "planner",
        },
    )
    graph.add_edge("run_baseline", "planner")

    graph.add_edge("planner", "evolver")
    graph.add_edge("evolver", "run_candidates")
    graph.add_edge("run_candidates", "evaluate")
    graph.add_edge("evaluate", "critic")

    # ── 条件边：critic → evolver(revision) 或 gate（E7a）──
    graph.add_conditional_edges(
        "critic",
        _route_after_critic,
        {
            "evolver": "evolver",  # revision 回环
            "gate": "gate",        # pass/reject
        },
    )

    # ── 条件边：gate → ship 或 loop_control ──
    graph.add_conditional_edges(
        "gate",
        _route_after_gate,
        {
            "ship": "ship",
            "loop_control": "loop_control",
        },
    )

    graph.add_edge("ship", "loop_control")

    # ── 条件边：loop_control → planner(下一轮) 或 END ──
    graph.add_conditional_edges(
        "loop_control",
        _route_loop,
        {
            "planner": "planner",
            END: END,
        },
    )

    return graph.compile()


# ── 路由函数（条件边的判断逻辑）──


def _route_start(state: AdaptState) -> str:
    """START 路由：round=0 跑基准，后续轮直接 planner。"""
    if state.get("round", 0) == 0 and not state.get("baseline_traces"):
        return "run_baseline"
    return "planner"


def _route_after_critic(state: AdaptState) -> str:
    """critic 后路由：revision（且 count<1）回 evolver，否则去 gate（E7a）。"""
    verdict = state.get("critic_verdict", {})
    v = verdict.get("verdict", "")
    revision_count = state.get("revision_count", 0)

    if v == "revision" and revision_count < 1:
        logger.info("round %d: critic 发起 revision（count=%d）", state.get("round", 0), revision_count)
        return "evolver"
    return "gate"


def _route_after_gate(state: AdaptState) -> str:
    """gate 后路由：shipped → ship，否则 → loop_control。"""
    outcome = state.get("round_outcome", "")
    if outcome == "shipped":
        return "ship"
    return "loop_control"


def _route_loop(state: AdaptState) -> str:
    """loop_control 路由：继续下一轮或结束（patience/budget，A11b）。"""
    if state.get("finished", False):
        return END

    round_num = state.get("round", 0)
    max_rounds = state.get("max_rounds", 3)
    idle_count = state.get("idle_count", 0)
    patience = state.get("patience", 2)

    # 预算用尽
    if round_num + 1 >= max_rounds:
        logger.info("adapt 结束：round %d 达 max_rounds %d", round_num, max_rounds)
        return END
    # patience 用尽
    if idle_count >= patience:
        logger.info("adapt 结束：idle %d 达 patience %d", idle_count, patience)
        return END

    return "planner"


__all__ = ["build_adapt_graph"]
