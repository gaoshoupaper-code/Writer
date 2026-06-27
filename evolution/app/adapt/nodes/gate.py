"""gate 节点 —— 确定性验收（Phase 8，Task 7.6）。

论文 §4.3：确定性门控，防 catastrophic forgetting（seesaw）。
不靠 LLM——纯数值判断：候选 reward vs 基准 reward + manifest 完整性。

seesaw 约束（A7b 基准快照对比）：候选不得明显退化基准。
"""
from __future__ import annotations

import logging

from app.adapt.state import AdaptState

logger = logging.getLogger("evolution.adapt.gate")

# seesaw 容忍度：候选 reward 不低于基准的此比例即算不退化（A7b 轻档妥协）
_SEESAW_TOLERANCE = 0.95


def gate(state: AdaptState) -> dict:
    """确定性验收：候选 vs 基准对比 → ship/reject。

    Returns: {round_outcome: "shipped"|"rejected", manifest 完整性检查}
    """
    verdict = state.get("critic_verdict", {})
    results = state.get("candidate_results", [])
    baseline_reward = state.get("baseline_reward", 0.0)

    v = verdict.get("verdict", "reject")
    if v == "reject":
        logger.info("round %d: gate reject（critic 判 reject）", state.get("round", 0))
        return {"round_outcome": "rejected"}

    if v == "revision":
        # revision 不经过 gate（critic → evolver 回环，E7a），不会到这里
        return {"round_outcome": "rejected"}

    # pass：取 ranking 第一的候选做 seesaw 检查
    ranking = verdict.get("ranking", [])
    if not ranking:
        return {"round_outcome": "rejected"}

    best_idx = ranking[0]
    if best_idx >= len(results):
        return {"round_outcome": "rejected"}

    best_reward = results[best_idx].get("reward", 0.0)

    # seesaw 约束：候选不得明显退化基准（A7b）
    threshold = baseline_reward * _SEESAW_TOLERANCE
    if best_reward < threshold:
        logger.info(
            "round %d: gate reject（seesaw: 候选 %.3f < 基准 %.3f × %.2f = %.3f）",
            state.get("round", 0), best_reward, baseline_reward, _SEESAW_TOLERANCE, threshold,
        )
        return {"round_outcome": "rejected"}

    # manifest 完整性检查（论文 §4.3 deterministic gate）
    if not _check_manifest_complete(state, best_idx):
        logger.info("round %d: gate reject（manifest 不完整）", state.get("round", 0))
        return {"round_outcome": "rejected"}

    # 通过：标为待 ship，记录 best_idx 供 ship 节点用
    logger.info("round %d: gate pass（候选 %d, reward %.3f >= %.3f）",
                state.get("round", 0), best_idx, best_reward, threshold)
    return {"round_outcome": "shipped", "critic_verdict": {**verdict, "ship_idx": best_idx}}


def _check_manifest_complete(state: AdaptState, candidate_idx: int) -> bool:
    """检查候选的 manifest 完整性（论文 §4.3）。"""
    candidates = state.get("candidates", [])
    if candidate_idx >= len(candidates):
        return False
    for edit in candidates[candidate_idx].get("edits", []):
        manifest = edit.get("manifest", {})
        if not manifest.get("intent"):
            return False
    return True


__all__ = ["gate"]
