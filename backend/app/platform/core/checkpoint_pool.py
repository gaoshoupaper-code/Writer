"""Checkpoint 分库连接池（D2 决策：每用户一个 SQLite）。

langgraph 的 AsyncSqliteSaver 现在改为每用户一个 checkpoints_<user_id>.db：
  - 物理隔离：删用户 = 删文件
  - 零侵入 langgraph：saver 实例本身不变，只是按 user_id 取不同实例
  - 惰性创建：首次请求某用户的 saver 时才建库

实现：
  - LRU 容量限制同时在飞的 saver 连接数（几十人规模 32 足够）
  - 线程安全（asyncio + lock）
  - 进程退出时统一关闭（aclose_all）
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


class CheckpointPool:
    """每用户 AsyncSqliteSaver 的惰性缓存（LRU）。"""

    def __init__(self, checkpoints_root: Path, max_open: int = 32) -> None:
        self.root = Path(checkpoints_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_open = max_open
        self._savers: OrderedDict[str, AsyncSqliteSaver] = OrderedDict()
        self._cms: dict[str, "AsyncSqliteSaver.from_conn_string"] = {}  # type: ignore[valid-type]
        self._lock = asyncio.Lock()

    def _db_path(self, user_id: str) -> Path:
        return self.root / f"checkpoints_{user_id}.db"

    async def get(self, user_id: str) -> AsyncSqliteSaver:
        """获取某用户的 saver，惰性创建。线程安全。"""
        async with self._lock:
            if user_id in self._savers:
                # LRU：移到末尾（最近使用）
                self._savers.move_to_end(user_id)
                return self._savers[user_id]

            # 驱逐最久未用（关闭其连接）
            while len(self._savers) >= self.max_open:
                evict_id, evict_cm = self._savers.popitem(last=False)
                try:
                    await evict_cm.__aexit__(None, None, None)
                except Exception:
                    pass
                self._cms.pop(evict_id, None)

            cm = AsyncSqliteSaver.from_conn_string(str(self._db_path(user_id)))
            saver = await cm.__aenter__()
            self._savers[user_id] = saver
            self._cms[user_id] = cm
            return saver

    async def drop(self, user_id: str) -> None:
        """删除某用户：关闭 saver + 删 db 文件。用于删用户/清理。"""
        async with self._lock:
            cm = self._cms.pop(user_id, None)
            saver = self._savers.pop(user_id, None)
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
        # 删除 db 文件（含 WAL/SHM 副本）
        db_path = self._db_path(user_id)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    async def aclose_all(self) -> None:
        """进程退出时统一关闭所有 saver。"""
        async with self._lock:
            cms = list(self._cms.values())
            self._savers.clear()
            self._cms.clear()
        for cm in cms:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass


# ── 单例 ───────────────────────────────────────────────────
_pool: CheckpointPool | None = None


def init_checkpoint_pool(pool: CheckpointPool) -> None:
    global _pool
    _pool = pool


def get_checkpoint_pool() -> CheckpointPool:
    if _pool is None:
        raise RuntimeError("CheckpointPool not initialized; call init_checkpoint_pool()")
    return _pool
