"""AdaptState —— adaptation loop 的状态 schema（Phase 8，Task 7.1，决策 E1a）。

LangGraph StateGraph 的 state：单一扁平 TypedDict，节点间流转。
历史数据（历轮 landscape/scores）不进 state（避免膨胀），进 DB（决策 E3a）。

设计依据：设计文档 E1a + adapt 数据流。
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

import operator


class Candidate(TypedDict):
    """一个候选 harness（Evolver 产出）。"""

    edits: list[dict]          # edit 指令列表（含 manifest，决策 A5a）
    config: dict               # apply edits 后的候选 config（内存，决策 D6a）
    source_commit: str         # 新源码的 git commit（D7a，无新源码=baseline commit）


class CandidateResult(TypedDict):
    """一个候选的执行 + 评估结果（run_candidates + evaluate 产出）。"""

    candidate_idx: int          # 对应 candidates 列表的索引
    trace_ids: list[str]        # 候选跑出的 trace_id 列表（batch 内）
    scores: dict[str, dict]     # per-trace 分数 {trace_id: {overall, std, samples, skipped}}
    reward: float               # batch 级聚合 reward（aggregate_scores 产出）


class AdaptState(TypedDict):
    """adaptation loop 的完整状态（单一扁平，决策 E1a）。

    生命周期：一个 adapt session（/api/adapt/start 触发）从头到尾。
    多轮（T=3-5）在同一 state 内迭代，每轮覆写当前轮字段。
    """

    # ── session 配置（init 写，整个 session 不变）──
    session_id: str             # adapt session uuid
    batch: list[dict]           # 固定测试集（A2a）
    baseline_config: dict       # 基准 config（E6a，启动时的 production）
    baseline_version: int       # 基准 config 的 harness_snapshots.version
    max_rounds: int             # T（A11b，默认 3-5）
    patience: int               # P（A11b，连续无改善退出，默认 2）
    judge_j: int                # verifier 打分次数（A3b，默认 3）

    # ── 当前轮状态（每轮覆写）──
    round: int                  # 当前轮次（0-based）
    baseline_traces: list[str]  # 基准 trace_id 列表（run_baseline 产出）
    baseline_scores: dict[str, dict]  # 基准 per-trace 分数（evaluate 产出）
    baseline_reward: float      # 基准 batch 级 reward（aggregate 产出）

    landscape: str              # 本轮 landscape（planner 产出，LLM 文本）

    candidates: list[Candidate]          # K 个候选（evolver 产出）
    revision_count: int          # revision 计数（≤1，决策 E7a）
    revision_feedback: str       # critic 的 revision 建议（revision 时填）
    revision_target: int         # 被修订的候选索引（E8a，-1=非 revision 模式）

    candidate_results: list[CandidateResult]  # 候选执行+评估结果

    critic_verdict: dict         # {verdict, ranking, feedback}（critic 产出）

    round_outcome: str           # shipped/rejected/idle（gate 产出）

    # ── 循环控制 ──
    idle_count: int              # 连续无改善轮数（patience P 用）
    best_reward: float           # 历史最佳 reward（判改善用）
    finished: bool               # loop 是否结束


def initial_state(
    session_id: str,
    batch: list[dict],
    baseline_config: dict,
    baseline_version: int,
    max_rounds: int = 3,
    patience: int = 2,
    judge_j: int = 3,
) -> AdaptState:
    """构建初始 AdaptState（init 节点用）。"""
    return AdaptState(
        session_id=session_id,
        batch=batch,
        baseline_config=baseline_config,
        baseline_version=baseline_version,
        max_rounds=max_rounds,
        patience=patience,
        judge_j=judge_j,
        round=0,
        baseline_traces=[],
        baseline_scores={},
        baseline_reward=0.0,
        landscape="",
        candidates=[],
        revision_count=0,
        revision_feedback="",
        revision_target=-1,
        candidate_results=[],
        critic_verdict={},
        round_outcome="",
        idle_count=0,
        best_reward=0.0,
        finished=False,
    )


__all__ = ["Candidate", "CandidateResult", "AdaptState", "initial_state"]
