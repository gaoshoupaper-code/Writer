"""进化 Agent 对话式 checkpointer 池（Phase 2A，决策 T1/T5）。

为对话式共创工作台提供 LangGraph checkpoint 持久化——每个 session 一个独立
SQLite 文件，LangGraph 通过 thread_id（= session_id）从 checkpoint 自动恢复
完整对话史，实现「按需触发模型」的多轮对话（决策 T2）。

设计：
  - per-session 物理隔离：evolve_<session_id>.db，discarded 时直接删文件（决策 T5）
  - 惰性创建：首次请求某 session 的 saver 时才建库
  - 单会话锁（决策 G）保证同时只会有 1-2 个活跃 session，无需 LRU 上限
  - 线程安全（asyncio.Lock）
  - 进程退出时统一关闭

与 executor 的 checkpoint_pool 差异：
  - executor：每用户一个（user_id 维度），LRU 容量 32
  - evolution：每 session 一个（session_id 维度），无 LRU（单会话锁保证不膨胀）

接口对齐 executor，方便未来共用模式。
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

logger = logging.getLogger("evolution.evolve.checkpoint_pool")


class EvolveCheckpointPool:
    """每 session 一个 AsyncSqliteSaver 的惰性缓存（决策 T5）。

    单会话锁（ACTIVE_STATUSES 同时只允许一个）保证同时活跃 session 数极少，
    因此不需要 LRU 容量限制——但保留 OrderedDict 结构便于未来扩展。
    """

    def __init__(self, checkpoints_root: Path) -> None:
        self.root = Path(checkpoints_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._savers: OrderedDict[str, AsyncSqliteSaver] = OrderedDict()
        self._cms: dict[str, object] = {}
        self._lock = asyncio.Lock()

    def _db_path(self, session_id: str) -> Path:
        """session 对应的 checkpoint db 路径。"""
        return self.root / f"evolve_{session_id}.db"

    async def get(self, session_id: str) -> AsyncSqliteSaver:
        """获取某 session 的 saver，惰性创建。线程安全。

        Args:
            session_id: 进化 session id（同时也是 LangGraph thread_id）
        Returns:
            该 session 专属的 AsyncSqliteSaver 实例。
        """
        async with self._lock:
            if session_id in self._savers:
                self._savers.move_to_end(session_id)
                return self._savers[session_id]

            # 单会话锁保证不会无限膨胀，但为防御性兜底，限制最多 8 个并发 saver
            # （正常 1 个，异常情况下最多 8 个，超过说明状态机出 bug）
            while len(self._savers) >= 8:
                evict_id, evict_cm = self._savers.popitem(last=False)
                evict_cm_closed = self._cms.pop(evict_id, None)
                if evict_cm_closed is not None:
                    try:
                        await evict_cm_closed.__aexit__(None, None, None)  # type: ignore[attr-defined]
                    except Exception:
                        logger.warning("关闭 saver %s 异常", evict_id, exc_info=True)
                logger.warning(
                    "checkpoint pool 超过 8 个 saver，驱逐最旧的 %s。"
                    "正常情况不应发生——检查单会话锁逻辑。",
                    evict_id,
                )

            cm = AsyncSqliteSaver.from_conn_string(str(self._db_path(session_id)))
            saver = await cm.__aenter__()
            self._savers[session_id] = saver
            self._cms[session_id] = cm
            logger.info("checkpoint saver 创建: session=%s", session_id)
            return saver

    async def drop(self, session_id: str) -> None:
        """删除某 session 的 checkpoint：关闭 saver + 删 db 文件。

        用于 discarded/failed session 的清理（决策 I/T5）。
        幂等：session 不存在也安全。
        """
        async with self._lock:
            cm = self._cms.pop(session_id, None)
            self._savers.pop(session_id, None)
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                logger.warning("关闭 saver %s 异常", session_id, exc_info=True)
        # 删除 db 文件（含 WAL/SHM 副本）
        db_path = self._db_path(session_id)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    logger.warning("删除 checkpoint 文件失败: %s", p, exc_info=True)

    async def aclose_all(self) -> None:
        """进程退出时统一关闭所有 saver。"""
        async with self._lock:
            cms = list(self._cms.values())
            self._savers.clear()
            self._cms.clear()
        for cm in cms:
            try:
                await cm.__aexit__(None, None, None)  # type: ignore[attr-defined]
            except Exception:
                pass


# ── 单例 ───────────────────────────────────────────────────
_pool: EvolveCheckpointPool | None = None


def init_checkpoint_pool(pool: EvolveCheckpointPool) -> None:
    """注入池实例（main.py lifespan 启动时调用）。"""
    global _pool
    _pool = pool


def get_checkpoint_pool() -> EvolveCheckpointPool:
    """取池实例。未初始化抛 RuntimeError。"""
    if _pool is None:
        raise RuntimeError(
            "EvolveCheckpointPool not initialized; call init_checkpoint_pool()"
        )
    return _pool


__all__ = [
    "EvolveCheckpointPool",
    "init_checkpoint_pool",
    "get_checkpoint_pool",
]
