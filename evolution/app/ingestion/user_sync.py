"""用户名映射同步：定时从 executor 拉取用户列表，写入本地 user_cache 表。

trace 历史观测功能：evolution 不维护用户主数据，靠定时拉取 executor 的
GET /internal/users（无鉴权，内网信任域），把 {user_id → username} 映射
缓存到本地。trace 历史列表查询时 LEFT JOIN user_cache 展示用户名。

同步策略：全量覆盖（DELETE + 批量 INSERT，原子操作）。executor 删了某用户，
本地也消失，不留孤儿残留。数据量小（<100 用户），全量覆盖最干净。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import app.core.db as db
from app.core.settings import settings

logger = logging.getLogger("evolution.user_sync")

_SYNC_INTERVAL = 86400.0  # 同步间隔：24 小时（每天一次）
_task: asyncio.Task | None = None


def start_user_sync_scheduler() -> None:
    """启动用户映射同步后台任务（幂等）。在 lifespan 启动时调用。"""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_sync_loop())


async def _sync_loop() -> None:
    """周期同步：启动先跑一次，之后每天刷新。"""
    # 启动先同步一次（接住 evolution 重启期间 executor 新注册的用户）
    await asyncio.to_thread(_sync_once)
    while True:
        await asyncio.sleep(_SYNC_INTERVAL)
        try:
            await asyncio.to_thread(_sync_once)
        except Exception:
            logger.exception("用户映射同步异常")


def _sync_once() -> int:
    """同步一次：从 executor 拉取用户列表，全量覆盖 user_cache。

    Returns:
        本次同步的用户数量。拉取失败返回 0（下次重试）。
    """
    import httpx

    url = f"{settings.executor_url}/internal/users"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        users = resp.json()
    except Exception as exc:
        logger.warning("用户映射同步：拉取用户列表失败：%s", exc)
        return 0

    if not users:
        return 0

    now = datetime.now(UTC).isoformat()
    conn = db.get_conn()
    with db._lock:
        # 全量覆盖：先清空再批量插入，保证与 executor 一致
        conn.execute("DELETE FROM user_cache")
        conn.executemany(
            "INSERT INTO user_cache (user_id, username, disabled, synced_at) VALUES (?, ?, ?, ?)",
            [
                (u["user_id"], u["username"], 1 if u.get("disabled") else 0, now)
                for u in users
            ],
        )
        conn.commit()

    logger.info("用户映射同步完成：%d 名用户", len(users))
    return len(users)
