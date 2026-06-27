"""runner 节点 —— 基准/候选执行 + 评估（Phase 8，Task 7.7）。

三个节点：
  run_baseline   跑基准 harness 在 batch 上 → baseline_traces
  run_candidates apply edits → HTTP /ab/run 轮询 → candidate_traces
  evaluate       多次打分（A3b）→ scores

跨服务调用 executor 的 /ab/run（异步轮询，决策 E5a）。evolution 编排（A8a）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.adapt.state import AdaptState
from app.core.settings import settings

logger = logging.getLogger("evolution.adapt.runner")

# /ab/status 轮询间隔（秒）
_POLL_INTERVAL = 10
# /ab/status 轮询超时（秒，单任务最大等待）
_POLL_TIMEOUT = 3600


def _executor_url(path: str) -> str:
    """拼接 executor 的 internal 端点 URL。"""
    return f"{settings.executor_url.rstrip('/')}{path}"


def _run_on_executor(
    config: dict,
    source_commit: str,
    batch_input: list[dict],
    baseline_version: int,
) -> list[str]:
    """调 executor /ab/run 跑候选，轮询 /ab/status 直到完成（E5a）。

    Returns: trace_id 列表（batch 内每个 input 一个 trace）
    """
    # 1. 发起异步任务
    resp = httpx.post(
        _executor_url("/internal/ab/run"),
        json={
            "config": config,
            "source_commit": source_commit,
            "batch_input": batch_input,
            "baseline_version": baseline_version,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    task_id = resp.json()["task_id"]
    logger.info("executor /ab/run 已启动: task=%s", task_id)

    # 2. 轮询 /ab/status 直到 done/failed（E5a）
    deadline = time.time() + _POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        status_resp = httpx.get(
            _executor_url(f"/internal/ab/status/{task_id}"),
            timeout=10.0,
        )
        if status_resp.status_code == 404:
            raise RuntimeError(f"executor task {task_id} not found")
        status_resp.raise_for_status()
        status_data = status_resp.json()

        if status_data["status"] == "done":
            trace_ids = status_data.get("trace_ids", [])
            logger.info("executor task %s done: %d traces", task_id, len(trace_ids))
            return trace_ids
        if status_data["status"] == "failed":
            raise RuntimeError(f"executor task {task_id} failed: {status_data.get('error')}")

    raise TimeoutError(f"executor task {task_id} 轮询超时（{_POLL_TIMEOUT}s）")


# ── 节点函数 ──────────────────────────────────────────────────────


def run_baseline(state: AdaptState) -> dict:
    """跑基准 harness 在 batch 上 → baseline_traces（E6a）。

    基准 config 从 state 读（启动时 init 写入）。source_commit 取基准快照的 commit。
    """
    config = state["baseline_config"]
    baseline_version = state["baseline_version"]
    batch = state["batch"]

    # 取基准的 source_commit（从 harness_snapshots 查）
    from app.improvement.snapshot_repo import get_snapshot_source_commit
    source_commit = get_snapshot_source_commit(baseline_version) or ""

    logger.info("round %d: 跑基准 v%d（commit=%s, batch=%d）",
                state.get("round", 0), baseline_version, source_commit, len(batch))

    trace_ids = _run_on_executor(config, source_commit, batch, baseline_version)
    return {"baseline_traces": trace_ids}


def run_candidates(state: AdaptState) -> dict:
    """apply edits → 跑 K 个候选 → candidate_traces（E8a revision 时只跑被修订的）。

    对每个候选：apply_edits 得 config → 调 executor 跑 → 收集 trace_ids。
    revision 模式（revision_target >= 0）只跑被修订的那一个。
    """
    from app.compose.edits import apply_edits

    candidates = state.get("candidates", [])
    batch = state["batch"]
    baseline_config = state["baseline_config"]
    revision_target = state.get("revision_target", -1)

    # E8a：revision 模式只跑被修订的候选
    if revision_target >= 0:
        indices = [revision_target]
        logger.info("round %d: revision 模式，只跑候选 %d", state.get("round", 0), revision_target)
    else:
        indices = list(range(len(candidates)))
        logger.info("round %d: 跑 %d 个候选", state.get("round", 0), len(candidates))

    results = []
    for idx in indices:
        cand = candidates[idx]
        # 候选 config 已经在 evolver 里 apply 过了（cand["config"]），直接用
        try:
            trace_ids = _run_on_executor(
                cand["config"], cand["source_commit"], batch, state["baseline_version"],
            )
            results.append({
                "candidate_idx": idx,
                "trace_ids": trace_ids,
                "scores": {},
                "reward": 0.0,
            })
        except Exception:
            logger.exception("候选 %d 执行失败", idx)
            results.append({
                "candidate_idx": idx,
                "trace_ids": [],
                "scores": {},
                "reward": 0.0,
            })

    return {"candidate_results": results}


def evaluate(state: AdaptState) -> dict:
    """对候选 trace 多次打分（A3b）→ candidate_scores + reward。

    基准 trace 也在此评估（第一次调用时）。
    """
    from app.adapt.verifier import score_trace, aggregate_scores

    judge_j = state.get("judge_j", 3)
    results = list(state.get("candidate_results", []))

    # 评估每个候选的 traces
    for result in results:
        trace_ids = result.get("trace_ids", [])
        scores = {tid: score_trace(tid, j=judge_j) for tid in trace_ids}
        result["scores"] = scores
        result["reward"] = aggregate_scores(scores)

    # 基准评估（baseline_traces 有值但 baseline_scores 为空时）
    baseline_traces = state.get("baseline_traces", [])
    baseline_scores = state.get("baseline_scores", {})
    baseline_reward = state.get("baseline_reward", 0.0)

    if baseline_traces and not baseline_scores:
        baseline_scores = {tid: score_trace(tid, j=judge_j) for tid in baseline_traces}
        baseline_reward = aggregate_scores(baseline_scores)
        logger.info("基准评估完成: reward=%.3f", baseline_reward)

    return {
        "candidate_results": results,
        "baseline_scores": baseline_scores,
        "baseline_reward": baseline_reward,
    }


__all__ = ["run_baseline", "run_candidates", "evaluate"]
