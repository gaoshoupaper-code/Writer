"""loop_control 节点 —— patience/budget 判断 + 轮次推进（Phase 8，Task 7.8）。

decide 继续/结束。更新 round/idle_count/best_reward/finished。
"""
from __future__ import annotations

import logging

from app.adapt.state import AdaptState

logger = logging.getLogger("evolution.adapt.loop_control")


def loop_control(state: AdaptState) -> dict:
    """判断是否继续下一轮（A11b），更新循环控制字段。

    Returns: state 更新（round+1, idle_count 调整, finished 设置）
    """
    round_num = state.get("round", 0)
    outcome = state.get("round_outcome", "")
    max_rounds = state.get("max_rounds", 3)
    patience = state.get("patience", 2)

    # 判断本轮是否改善
    best = state.get("best_reward", 0.0)
    # 取候选中最好的 reward
    results = state.get("candidate_results", [])
    current_best = max((r.get("reward", 0) for r in results), default=0.0)

    improved = current_best > best
    new_best = max(best, current_best)

    idle = state.get("idle_count", 0)
    if improved:
        idle = 0
    else:
        idle += 1

    next_round = round_num + 1
    # 软停检查（D12）：前端请求停止 → 下轮检查时直接结束
    stop_requested = False
    try:
        from app.adapt import events
        stop_requested = events.is_stop_requested(state.get("session_id", ""))
    except Exception:
        pass
    finished = next_round >= max_rounds or idle >= patience or stop_requested

    logger.info(
        "loop_control: round %d→%d, outcome=%s, improved=%s, idle=%d/%d, finished=%s",
        round_num, next_round, outcome, improved, idle, patience, finished,
    )

    return {
        "round": next_round,
        "idle_count": idle,
        "best_reward": new_best,
        "finished": finished,
        # 清理本轮临时状态，为下一轮准备
        "revision_count": 0,
        "revision_feedback": "",
        "revision_target": -1,
        "critic_verdict": {},
    }


__all__ = ["loop_control"]
