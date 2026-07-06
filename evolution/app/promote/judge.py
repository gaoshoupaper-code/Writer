"""Judge 调用（数据闭环设计 B3）。

复用 eval_agent/scoring.evaluate_trace 的打分结果，转译为 promote 闸门的 verdict。

流程：
  1. filter.check_trace 先过滤明显垃圾（省 judge 成本）
  2. 调 scoring.evaluate_trace（幂等：已评过会跳过，复用 evaluation_runs 记录）
  3. 从 scoring 结果提取内容层 overall 分数
  4. 按阈值判定 verdict：auto_promote / needs_human / auto_reject
  5. 写 promote_tasks

judge 复用 eval_agent 的打分链路（不重复造），但 promote_tasks 只存"入不入数据集"
的决策摘要，详细分数在 evaluation_scores 表。
"""
from __future__ import annotations

import logging
from typing import Any

from app.promote import repo
from app.promote import filter as rule_filter

logger = logging.getLogger("evolution.promote.judge")


def judge_trace(trace_id: str) -> str | None:
    """对一条 trace 跑 filter + judge，推进 promote_task 状态。

    Returns:
        推进后的 promote_task 状态（pending/judging/needs_confirm/rejected）；
        trace 无对应 promote_task 或异常返回 None。
    """
    task = repo.get_by_trace(trace_id)
    if task is None:
        logger.warning("judge_trace: trace %s 无 promote_task", trace_id)
        return None

    # 已终态（rejected/promoted）跳过
    if task["status"] in (repo.STATUS_REJECTED, repo.STATUS_PROMOTED):
        return task["status"]

    repo.mark_judging(task["task_id"])

    # 1. 规则过滤（省 judge 成本）
    passed, reject_reason, detail = rule_filter.filter_and_decide(trace_id)
    if not passed:
        logger.info("promote %s 规则过滤淘汰：%s", trace_id, reject_reason)
        repo.set_judge_result(
            task["task_id"],
            scores={"filtered": True, "violations": detail["violations"]},
            verdict=repo.VERDICT_AUTO_REJECT,
        )
        return repo.STATUS_REJECTED

    # 2. 调 eval_agent/scoring（幂等：已评过复用结果）
    from app.eval_agent import scoring
    result = scoring.evaluate_trace(trace_id)

    if result is None:
        # scoring 跳过（LLM 未配置或已评估但无结果）
        logger.warning("promote %s judge 跳过（scoring 返回 None）", trace_id)
        # 回退到 pending，等下次重试或人工处理
        import app.core.db as db
        db.execute(
            "UPDATE promote_tasks SET status=? WHERE task_id=?",
            (repo.STATUS_PENDING, task["task_id"]),
        )
        return repo.STATUS_PENDING

    # 3. 提取分数摘要 + 判定 verdict
    scores_summary = _extract_scores_summary(result)
    verdict = _decide_verdict(scores_summary["content_overall"])

    repo.set_judge_result(
        task["task_id"],
        scores=scores_summary,
        verdict=verdict,
    )
    logger.info(
        "promote %s judge 完成：overall=%.2f verdict=%s",
        trace_id, scores_summary["content_overall"], verdict,
    )

    return repo.STATUS_NEEDS_CONFIRM if verdict != repo.VERDICT_AUTO_REJECT else repo.STATUS_REJECTED


def _extract_scores_summary(eval_result: dict[str, Any]) -> dict[str, Any]:
    """从 scoring.evaluate_trace 的返回提取分数摘要。

    eval_result 格式：{content: {overall, scores, ...}, subagent: {...}, badcase: {...}}
    """
    content = eval_result.get("content", {})
    subagent = eval_result.get("subagent", {})
    badcase = eval_result.get("badcase", {})

    # 内容层 overall（scoring 的 _evaluate_content_layer 返回）
    content_overall = float(content.get("overall", 0)) if not content.get("skipped") else 0.0

    # subagent 各 agent 的分数
    subagent_scores: dict[str, float] = {}
    for agent, res in subagent.items():
        if not res.get("skipped"):
            subagent_scores[agent] = float(res.get("score", 0))

    return {
        "content_overall": content_overall,
        "content_scores": content.get("scores", {}),
        "subagent_scores": subagent_scores,
        "is_badcase": badcase.get("is_badcase", False),
        "flagged_count": len(badcase.get("flagged_dimensions", [])),
    }


def _decide_verdict(content_overall: float) -> str:
    """根据内容层 overall 分数判定 verdict（阈值见 repo.py）。"""
    if content_overall >= repo.AUTO_PROMOTE_THRESHOLD:
        return repo.VERDICT_AUTO_PROMOTE
    if content_overall < repo.AUTO_REJECT_THRESHOLD:
        return repo.VERDICT_AUTO_REJECT
    return repo.VERDICT_NEEDS_HUMAN


__all__ = ["judge_trace"]
