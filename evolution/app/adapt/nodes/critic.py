"""critic 节点 —— 读 scores+manifest → verdict（Phase 8，Task 7.5）。

论文 §4.3：防 reward hacking。评估每个候选的 manifest vs trace 证据，
产出 verdict: pass/reject/revision。最多 1 次 revision（E7a）。

Critic 是 LLM 驱动的（读 manifest + 分数判断），gate 是确定性的。
"""
from __future__ import annotations

import json
import logging

from app.adapt.state import AdaptState
from app.core import llm

logger = logging.getLogger("evolution.adapt.critic")


def critic(state: AdaptState) -> dict:
    """读候选 scores + manifest → LLM 判 verdict。

    Returns: {critic_verdict: {verdict, ranking, feedback, target_idx}}
    """
    results = state.get("candidate_results", [])
    candidates = state.get("candidates", [])
    baseline_reward = state.get("baseline_reward", 0.0)

    if not results:
        return {"critic_verdict": {"verdict": "reject", "reason": "无候选结果"}}

    if not llm.judge_enabled():
        # 无 LLM：纯按 reward 排序，取最高，无 revision
        best_idx = max(range(len(results)), key=lambda i: results[i].get("reward", 0))
        best_reward = results[best_idx].get("reward", 0)
        verdict = "pass" if best_reward > baseline_reward else "reject"
        return {"critic_verdict": {"verdict": verdict, "ranking": [best_idx], "target_idx": best_idx}}

    # LLM 驱动：读 manifest + scores 判断
    ranking, verdict, feedback, target_idx = _llm_judge(results, candidates, baseline_reward)

    logger.info("round %d: critic verdict=%s, ranking=%s", state.get("round", 0), verdict, ranking)
    return {"critic_verdict": {"verdict": verdict, "ranking": ranking, "feedback": feedback, "target_idx": target_idx}}


def _llm_judge(results, candidates, baseline_reward):
    """LLM 读 manifest + 分数，产 ranking + verdict + feedback。"""
    summaries = []
    for i, result in enumerate(results):
        reward = result.get("reward", 0)
        manifest_parts = []
        for edit in candidates[i].get("edits", []):
            m = edit.get("manifest", {})
            manifest_parts.append(f"    {m.get('intent', '?')}: 预期涨{m.get('expected_up', [])}, 预期跌{m.get('expected_down', [])}")
        summaries.append(f"候选 {i}: reward={reward:.3f}\n" + "\n".join(manifest_parts))

    prompt = f"""## 基准 reward: {baseline_reward:.3f}

## 候选
{chr(10).join(summaries)}

## 你的任务
1. 按"manifest 声称 vs 实际分数"判断每个候选是否有 reward hacking（声称改进但实际作弊）
2. 排序候选（best first）
3. 决定 verdict: pass（最佳候选可上线）/ reject（都不行）/ revision（最佳方向对但需修订）
输出 JSON: {{"ranking": [idx...], "verdict": "pass|reject|revision", "feedback": "...", "target_idx": best_idx}}
"""
    raw = llm.chat([{"role": "user", "content": prompt}]) or "{}"
    try:
        data = json.loads(_strip(raw))
        return data.get("ranking", []), data.get("verdict", "reject"), data.get("feedback", ""), data.get("target_idx", 0)
    except json.JSONDecodeError:
        # 降级：纯按 reward
        best = max(range(len(results)), key=lambda i: results[i].get("reward", 0))
        return [best], "pass" if results[best].get("reward", 0) > baseline_reward else "reject", "", best


def _strip(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        import re
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t


__all__ = ["critic"]
