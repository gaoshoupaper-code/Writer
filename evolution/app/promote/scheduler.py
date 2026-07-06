"""judge_scheduler 后台调度（数据闭环设计 B4/A7）。

定时扫描两步：
  1. 发现：runs 表里 run_purpose='user_generation' 且 status='completed' 且
     无 promote_task 的 trace → 自动创建 promote_task(pending)
  2. judge：对 pending 任务跑 filter + scoring.evaluate_trace → 推进状态

风格对齐 scan.py：asyncio.create_task + asyncio.to_thread（不阻塞事件循环）。
"""
from __future__ import annotations

import asyncio
import logging

import app.core.db as db

logger = logging.getLogger("evolution.promote.scheduler")

_SCAN_INTERVAL = 300.0  # 5 分钟扫一次
_task: asyncio.Task | None = None


def start_judge_scheduler() -> None:
    """启动 judge 调度器（幂等）。在 lifespan 启动时调用。"""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_judge_loop())


async def _judge_loop() -> None:
    """周期扫描：发现未建任务的生产 trace + judge pending 任务。"""
    # 启动后先跑一次
    await asyncio.to_thread(_scan_once)
    while True:
        await asyncio.sleep(_SCAN_INTERVAL)
        try:
            await asyncio.to_thread(_scan_once)
        except Exception:
            logger.exception("promote judge 扫描异常")


def _scan_once() -> dict[str, int]:
    """扫描一次，返回 {discovered, judged}。

    discovered: 新建 promote_task 数
    judged:     完成 judge 的任务数
    """
    from app.promote import repo, judge

    # 1. 发现未建任务的生产 trace
    discovered = _discover_pending_tasks()

    # 2. judge pending 任务
    pending = repo.list_pending_judge(limit=10)  # 每轮限 10 个（控制 judge 成本）
    judged = 0
    for task in pending:
        try:
            status = judge.judge_trace(task["trace_id"])
            if status:
                judged += 1
        except Exception:
            logger.exception("judge_trace 异常 %s", task["trace_id"])

    if discovered or judged:
        logger.info(
            "promote 扫描：发现 %d 条新任务，judge %d 条", discovered, judged
        )
    return {"discovered": discovered, "judged": judged}


def _discover_pending_tasks(limit: int = 50) -> int:
    """发现未建 promote_task 的生产 trace，自动创建。

    条件：run_purpose='user_generation' + status='completed' + 无 promote_task。
    """
    # 找 completed 生产 trace 且无 promote_task 的
    rows = db.query_all(
        """SELECT r.trace_id, r.owner_user_id FROM runs r
           WHERE r.run_purpose = 'user_generation'
             AND r.status = 'completed'
             AND NOT EXISTS (
                 SELECT 1 FROM promote_tasks pt WHERE pt.trace_id = r.trace_id
             )
           ORDER BY r.ingested_at DESC
           LIMIT ?""",
        (limit,),
    )
    if not rows:
        return 0

    from app.promote import repo
    count = 0
    for row in rows:
        repo.create_task(
            trace_id=row["trace_id"],
            owner_user_id=row["owner_user_id"],
        )
        count += 1
    logger.info("发现 %d 条未建任务的生产 trace", count)
    return count


__all__ = ["start_judge_scheduler"]
