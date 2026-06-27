"""活跃 trace 轮询（Phase 6 T15）。

定期轮询执行端 /internal/active-runs，缓存到内存（不存 DB，D22 进行中不入库）。
页面读内存缓存展示活跃大盘。

设计依据：T15（轮询拉取，只展示不存储）+ D21（只看活跃大盘）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter

from app.core.settings import settings

logger = logging.getLogger("evolution.active")

router = APIRouter(tags=["active"])

# 执行端 internal 接点（拉活跃 trace）。
# 执行端地址优先用 evolution 配置里的 executor_url，否则默认 localhost:8000。
_POLL_INTERVAL = 5.0  # 轮询间隔（秒）

# 内存缓存（进程级，重启丢失——无妨，活跃大盘是实时观测，不需持久）。
_active_cache: list[dict[str, Any]] = []
_task: asyncio.Task | None = None


def start_active_poller() -> None:
    """启动轮询后台任务（幂等）。在 lifespan 启动时调用。"""
    global _task
    executor_url = getattr(settings, "executor_url", "") or "http://localhost:8000"
    if _task is None or _task.done():
        _task = asyncio.create_task(_poll_loop(executor_url))


def get_active_runs() -> list[dict[str, Any]]:
    """读取缓存的活跃 trace列表（页面用）。"""
    return list(_active_cache)


async def _poll_loop(executor_url: str) -> None:
    """周期轮询执行端活跃 trace。失败静默（执行端不可用不影响 evolution）。"""
    while True:
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            await asyncio.to_thread(_poll_once, executor_url)
        except Exception:
            logger.debug("活跃 trace 轮询失败", exc_info=True)


def _poll_once(executor_url: str) -> None:
    """轮询一次执行端 /internal/active-runs。"""
    global _active_cache
    try:
        import httpx

        url = f"{executor_url.rstrip('/')}/internal/active-runs"
        resp = httpx.get(url, timeout=3.0)
        resp.raise_for_status()
        _active_cache = resp.json()
    except Exception:
        # 执行端不可用 → 清空缓存（活跃大盘显示空，不报错）
        _active_cache = []


# ── D7：富化 JSON 端点（供监测前端轮询）──


@router.get("/active-runs")
def active_runs_api() -> list[dict[str, Any]]:
    """活跃 trace 富化列表（D7）。

    薄包装 get_active_runs()（active_poller 缓存的 executor 原始数据），
    join evolution.db runs 表补 session_name + ingested 标记。

    未摄入的活跃 trace（HITL awaiting_input 首次摄入空窗）join 不到 →
    session_name=null 降级，ingested=false。
    """
    runs = get_active_runs()
    if not runs:
        return []

    # 批量查 evolution.db，一次拿全部活跃 trace_id 的 session_name（避免 N 次 IO）
    import app.core.db as db

    trace_ids = [r.get("trace_id", "") for r in runs if r.get("trace_id")]
    enriched: list[dict[str, Any]] = []
    if trace_ids:
        placeholders = ",".join("?" * len(trace_ids))
        rows = db.query_all(
            f"SELECT trace_id, session_name FROM runs WHERE trace_id IN ({placeholders})",
            tuple(trace_ids),
        )
    else:
        rows = []
    ingested_map = {r["trace_id"]: r.get("session_name") for r in rows}

    for r in runs:
        tid = r.get("trace_id", "")
        session_name = ingested_map.get(tid)
        enriched.append({
            "trace_id": tid,
            "workspace_id": r.get("workspace_id", ""),
            "thread_id": r.get("thread_id"),
            "endpoint": r.get("endpoint"),
            "status": r.get("status", "running"),
            "started_at": r.get("started_at"),
            "duration_ms": r.get("duration_ms"),
            "event_count": r.get("event_count", 0),
            # D7 富化：join 不到时 null（前端降级显示 workspace_id/endpoint）
            "session_name": session_name,
            "ingested": tid in ingested_map,
        })
    return enriched
