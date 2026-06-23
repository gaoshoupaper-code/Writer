"""活跃 trace 轮询（Phase 6 T15）。

定期轮询执行端 /internal/active-runs，缓存到内存（不存 DB，D22 进行中不入库）。
页面读内存缓存展示活跃大盘。

设计依据：T15（轮询拉取，只展示不存储）+ D21（只看活跃大盘）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.settings import settings

logger = logging.getLogger("evolution.active")

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
