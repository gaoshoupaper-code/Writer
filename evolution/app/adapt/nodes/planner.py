"""planner 节点 —— 读 trace + 历史 → 产 landscape（Phase 8，Task 7.3）。

防 under-exploration（论文 §4.2）：构建 adaptation landscape，让 Evolver 不只盯
当前失败 trace，看全局（哪些失败模式、试过什么、哪些方向没试过）。

查 DB 历史（决策 E3a）+ 当前轮 baseline trace 摘要 → LLM 产 landscape。
"""
from __future__ import annotations

import json
import logging

from app.adapt.state import AdaptState
from app.core import llm

logger = logging.getLogger("evolution.adapt.planner")


def planner(state: AdaptState) -> dict:
    """读 baseline trace + DB 历史 → LLM 产 landscape。

    Returns: {landscape: str}
    """
    if not llm.judge_enabled():
        logger.warning("planner 跳过：LLM 未配置，用空 landscape")
        return {"landscape": "LLM 未配置，无 landscape"}

    # 读历史（E3a）
    history = _load_history(state["session_id"])

    # 读当前轮 baseline 分数摘要
    baseline_scores = state.get("baseline_scores", {})
    baseline_summary = _summarize_scores(baseline_scores)

    prompt = _build_prompt(state, baseline_summary, history)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    landscape = llm.chat(messages) or ""

    logger.info("round %d: landscape 产出（%d 字符）", state.get("round", 0), len(landscape))
    return {"landscape": landscape}


def _load_history(session_id: str) -> list[dict]:
    """从 adapt_rounds 表读历轮历史（E3a）。"""
    import app.core.db as db

    rows = db.query_all(
        """SELECT round, landscape, round_outcome, candidate_scores, critic_verdict
           FROM adapt_rounds WHERE session_id=? ORDER BY round DESC LIMIT 5""",
        (session_id,),
    )
    return [dict(r) for r in rows] if rows else []


def _summarize_scores(scores: dict) -> str:
    """把 per-trace 分数摘要成文本。"""
    if not scores:
        return "无分数（首轮或未评估）"
    lines = []
    for tid, s in scores.items():
        if isinstance(s, dict) and not s.get("skipped"):
            lines.append(f"  {tid}: overall={s.get('overall', 0):.3f}±{s.get('std', 0):.3f}")
    return "\n".join(lines) or "全部跳过"


def _build_prompt(state: AdaptState, baseline_summary: str, history: list[dict]) -> str:
    """构建 planner 的 user prompt。"""
    round_num = state.get("round", 0)
    batch_desc = "\n".join(f"  - {b['id']}: {b['genre']} - {b['premise'][:40]}" for b in state["batch"])

    history_text = "无历史（首轮）"
    if history:
        history_text = "\n".join(
            f"  round {h['round']}: {h.get('round_outcome', '?')}"
            for h in history
        )

    return f"""## 当前轮次：Round {round_num}

## 测试集（batch）
{batch_desc}

## 基准分数
{baseline_summary}

## 历史轮次
{history_text}

## 你的任务
分析当前 baseline 的失败模式，产出 adaptation landscape：
1. 主要失败模式（跨 trace 的共性 vs 个性）
2. 历史已试过的改进方向（避免重复）
3. 未试过的改进方向（鼓励探索结构性改动，不只 prompt 微调）

输出纯文本 landscape 分析。
"""


_SYSTEM_PROMPT = """你是 HarnessX 的 Planner。你的职责是构建 adaptation landscape：
分析当前 harness 在测试集上的失败模式，为下游 Evolver 提供"该往哪个方向改"的全景视图。

你是防 under-exploration 的关键——不要只盯着当前失败做局部修补，
要鼓励 Evolver 探索结构性改动（新 middleware、改控制流、调参数等）。

输出要求：纯文本分析，分"失败模式 / 已试方向 / 未试方向"三段。"""


__all__ = ["planner"]
