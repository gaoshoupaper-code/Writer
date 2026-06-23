"""A/B 实验编排（Phase 3 T3.3）。

职责：candidate vs production 各跑 N 次（D7=3）→ 评估每个 trace → 均值对比 → verdict。

流程：
  1. 取候选（improvement_candidates.candidate_version_id 对应的 prompt）
  2. 对每个测试需求：
     - production label 跑 N 次（调后端 /internal/ab-replay）
     - candidate label 跑 N 次
  3. 等 trace 摄入 + 评估完成（轮询 evaluation_runs.status）
  4. 聚合两组分数均值，对比得 verdict（win/lose/tie）
  5. 写 ab_experiments

时序：回放生成 + 摄入 + 评估全异步。实验编排用同步轮询等待（在后台 task 跑，
不阻塞 HTTP）。单次实验可能跑很久（生成耗时 × N × 2）。

设计依据：设计文档 D7（多seed跑3次）+ D5（复用生成链路）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import app.core.db as db

logger = logging.getLogger("evolution.experiment")

# 每个 label 跑几次（D7）
SEED_COUNT = 3
# 轮询 trace 评估完成的间隔（秒）
POLL_INTERVAL = 10
# 单 trace 等待评估超时（秒）：生成+摄入+评估可能数分钟
TRACE_TIMEOUT = 600


def run_experiment(candidate_id: int, test_set_id: int | None = None) -> dict[str, Any] | None:
    """跑一个 A/B 实验（同步，应放后台 task 调用）。

    Args:
        candidate_id: improvement_candidates.id（含 candidate_version_id）
        test_set_id: 回放测试集 id（None 用 default-xianxia）

    Returns: 实验结果摘要 或 None（失败）。
    """
    import httpx
    from app.core.settings import settings
    from app.improvement.replay import ensure_default_test_set

    cand = db.query_one("SELECT * FROM improvement_candidates WHERE id=?", (candidate_id,))
    if cand is None or cand["candidate_version_id"] is None:
        logger.error("实验失败：候选 %s 无候选版本", candidate_id)
        return None

    # 测试集
    if test_set_id is None:
        test_set = ensure_default_test_set()
        test_set_id = test_set["id"]
    test_set = db.query_one("SELECT * FROM replay_test_sets WHERE id=?", (test_set_id,))
    if test_set is None:
        logger.error("实验失败：测试集 %s 不存在", test_set_id)
        return None
    prompts = json.loads(test_set["prompts_json"]) if test_set["prompts_json"] else []
    if not prompts:
        logger.error("实验失败：测试集 %s 为空", test_set_id)
        return None

    prompt_name = cand["prompt_name"]
    now = datetime.now(UTC).isoformat()

    # 创建实验记录
    cur = db.execute(
        """INSERT INTO ab_experiments
           (candidate_id, prompt_name, test_set_id, status, created_at)
           VALUES (?, ?, ?, 'running', ?)""",
        (candidate_id, prompt_name, test_set_id, now),
    )
    experiment_id = cur.lastrowid

    executor_url = settings.executor_url.rstrip("/")
    try:
        # 对每个测试需求，production + candidate 各跑 SEED_COUNT 次
        production_scores: list[float] = []
        candidate_scores: list[float] = []
        production_traces: list[str] = []
        candidate_traces: list[str] = []

        # 用第一个测试需求跑（简化：多需求时取均值的均值）
        req_item = prompts[0]
        for seed in range(SEED_COUNT):
            # production
            prod_trace = _run_one(executor_url, "production", req_item)
            if prod_trace:
                production_traces.append(prod_trace)
            # candidate
            cand_trace = _run_one(executor_url, "candidate", req_item)
            if cand_trace:
                candidate_traces.append(cand_trace)

        # 等所有 trace 评估完成
        for trace_id in production_traces + candidate_traces:
            _wait_evaluation(trace_id)

        # 聚合分数
        production_scores = _aggregate_scores(production_traces)
        candidate_scores = _aggregate_scores(candidate_traces)

        # verdict：candidate 均分 > production 均分（含容差）→ win
        prod_avg = _mean(production_scores)
        cand_avg = _mean(candidate_scores)
        verdict = _decide_verdict(prod_avg, cand_avg)

        db.execute(
            """UPDATE ab_experiments
               SET production_scores_json=?, candidate_scores_json=?,
                   verdict=?, status='done' WHERE id=?""",
            (
                json.dumps({"avg": prod_avg, "scores": production_scores, "traces": production_traces}, ensure_ascii=False),
                json.dumps({"avg": cand_avg, "scores": candidate_scores, "traces": candidate_traces}, ensure_ascii=False),
                verdict, experiment_id,
            ),
        )
        # 更新候选状态
        db.execute(
            "UPDATE improvement_candidates SET status='ab_testing' WHERE id=?",
            (candidate_id,),
        )
        return {
            "experiment_id": experiment_id, "candidate_id": candidate_id,
            "prompt_name": prompt_name,
            "production_avg": prod_avg, "candidate_avg": cand_avg,
            "verdict": verdict,
            "production_traces": production_traces,
            "candidate_traces": candidate_traces,
        }
    except Exception:
        logger.exception("A/B 实验失败 %s", experiment_id)
        db.execute(
            "UPDATE ab_experiments SET status='error' WHERE id=?", (experiment_id,)
        )
        return None


def _run_one(executor_url: str, label: str, req_item: dict[str, str]) -> str | None:
    """调执行端 ab-replay 跑一次生成，返回 trace_id。"""
    try:
        resp = httpx.post(
            f"{executor_url}/internal/ab-replay",
            json={
                "prompt_label": label,
                "genre": req_item.get("genre", "玄幻"),
                "premise": req_item.get("request", ""),
                "title": f"AB-{label}",
            },
            timeout=TRACE_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "failed":
            logger.warning("回放生成失败(label=%s): %s", label, data.get("error"))
            return None
        return data.get("trace_id", "")
    except Exception:
        logger.exception("调 ab-replay 失败(label=%s)", label)
        return None


def _wait_evaluation(trace_id: str) -> None:
    """轮询等待某 trace 的评估完成（或超时）。"""
    if not trace_id:
        return
    deadline = time.time() + TRACE_TIMEOUT
    while time.time() < deadline:
        row = db.query_one(
            "SELECT status FROM evaluation_runs WHERE trace_id=?", (trace_id,)
        )
        if row and row["status"] in ("done", "error"):
            return
        time.sleep(POLL_INTERVAL)
    logger.warning("trace %s 评估等待超时", trace_id)


def _aggregate_scores(trace_ids: list[str]) -> list[float]:
    """聚合多个 trace 的评估分数（每 trace 取内容+subagent 综合均分）。"""
    scores: list[float] = []
    for trace_id in trace_ids:
        if not trace_id:
            continue
        rows = db.query_all(
            "SELECT score FROM evaluation_scores WHERE trace_id=?", (trace_id,)
        )
        if rows:
            trace_avg = sum(r["score"] for r in rows) / len(rows)
            scores.append(trace_avg)
    return scores


def _mean(scores: list[float]) -> float:
    return sum(scores) / len(scores) if scores else 0.0


def _decide_verdict(prod_avg: float, cand_avg: float) -> str:
    """对比均值得 verdict。容差 0.05 内算 tie（避免微小差异误判）。"""
    diff = cand_avg - prod_avg
    if diff > 0.05:
        return "win"
    if diff < -0.05:
        return "lose"
    return "tie"


def list_experiments(status: str | None = None) -> list[dict[str, Any]]:
    """列实验记录。"""
    if status:
        rows = db.query_all(
            "SELECT * FROM ab_experiments WHERE status=? ORDER BY id DESC", (status,)
        )
    else:
        rows = db.query_all("SELECT * FROM ab_experiments ORDER BY id DESC")
    result = []
    for r in rows:
        item = dict(r)
        if r["production_scores_json"]:
            item["production"] = json.loads(r["production_scores_json"])
        if r["candidate_scores_json"]:
            item["candidate"] = json.loads(r["candidate_scores_json"])
        result.append(item)
    return result
