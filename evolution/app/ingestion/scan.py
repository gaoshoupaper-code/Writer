"""兜底扫描：定时调执行端列表 API，补摄入漏通知的 trace。

Phase 3 重构：不再扫执行端文件系统（glob workspace 目录），改调
GET /internal/traces 列表端点拿近期 trace 清单，对未摄入的逐个拉取。

设计：执行端通知可能丢失（网络/evolution 未启动），靠定时扫描保证最终一致。
判断"已摄入"：runs 表已有该 trace_id。
"""

from __future__ import annotations

import asyncio
import logging

import app.core.db as db
from app.core.settings import settings

logger = logging.getLogger("evolution.scan")

_SCAN_INTERVAL = 60.0  # 扫描间隔（秒）
_task: asyncio.Task | None = None


def start_scan_scheduler() -> None:
    """启动兜底扫描后台任务（幂等）。在 lifespan 启动时调用。"""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_scan_loop())


async def _scan_loop() -> None:
    """周期扫描：找漏通知的 trace 补摄入。"""
    # 启动后先扫一次（接住 evolution 重启期间漏的）
    await asyncio.to_thread(_scan_once)
    while True:
        await asyncio.sleep(_SCAN_INTERVAL)
        try:
            await asyncio.to_thread(_scan_once)
        except Exception:
            logger.exception("兜底扫描异常")


def _scan_once() -> int:
    """扫描一次，返回本次补摄入的数量。

    庚方案（D9）：调执行端 GET /internal/traces 拿近期 trace 清单（带 status），
    逐条对比 evolution runs 表的 status：
    - evolution 没有 → 新 trace，拉取摄入
    - status 不一致 → 状态变迁（如 awaiting_input→completed），重拉摄入
    - status 一致 → 跳过
    """
    import httpx

    url = f"{settings.executor_url}/internal/traces"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        recent_traces = data.get("traces", [])
    except Exception as exc:
        logger.warning("兜底扫描：拉取 trace 列表失败：%s", exc)
        return 0

    if not recent_traces:
        return 0

    # 已摄入的 trace_id → status 映射（用于变迁检测）
    ingested_rows = db.query_all("SELECT trace_id, status FROM runs")
    ingested_status = {r["trace_id"]: r["status"] for r in ingested_rows}
    count = 0
    for item in recent_traces:
        trace_id = item.get("trace_id", "")
        if not trace_id:
            continue
        executor_status = item.get("status", "")
        local_status = ingested_status.get(trace_id)
        # 跳过条件：已摄入且 status 一致（无变迁）
        if local_status is not None and local_status == executor_status:
            continue
        # 新 trace 或 status 变迁 → 拉取摄入
        tid = _fetch_and_ingest(trace_id, item.get("workspace_id"))
        if tid:
            count += 1
            logger.info("兜底摄入: %s (变更: %s→%s)", tid, local_status, executor_status)
    return count


def _fetch_and_ingest(trace_id: str, workspace_hint: str | None) -> str | None:
    """拉取单个 trace 内容并摄入（兜底扫描专用）。"""
    from app.ingestion.ingestion import _fetch_trace_content
    from app.ingestion import importer

    fetched = _fetch_trace_content(trace_id)
    if fetched is None:
        return None
    events, hint, run_status_hint = fetched
    # 优先用列表端点返回的 workspace_id；run_status_hint 用于 importer 纠正运行中误判
    return importer.ingest_events(
        events, workspace_hint or hint, run_status_hint=run_status_hint
    )
