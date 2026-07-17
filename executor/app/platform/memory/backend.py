"""MemoryBackend — NWM 记忆系统门面（对 harness 透明的检索入口）。

去 Graphiti 重构（2026-07-17）：薄门面，组合 store + embedder + retriever。
保留旧接口 retrieve/health_check，harness 的 MemoryRecallMiddleware 无需改动调用方式。

与旧版的关键差异：
  - 旧版是进程单例（一个 FalkorDB），靠 group_id 区分作品。
  - 新版是 per-workspace 实例（持 workspace_id），retrieve 时从 MemoryStorePool 取对应 store。
    一作品一 memory.db 文件（D-D2-2），物理隔离。

retrieve 内部编排论文 §A.1 四阶段（causal cutoff + hybrid RRF + one-hop JOIN + bounded packet），
实际由 MemoryRetriever 执行，本类只负责取 store + embed query + 转发。

设计依据：设计文档 D-D2-1（MemoryBackend 门面）、D-D4-1（retrieve 签名兼容）。
"""
from __future__ import annotations

import logging
from typing import Any

from app.platform.memory.embedder import MemoryEmbedError, get_memory_embedder
from app.platform.memory.retriever import EvidencePacket, MemoryRetriever, get_memory_retriever
from app.platform.memory.store import get_memory_store_pool

logger = logging.getLogger(__name__)


class MemoryBackend:
    """记忆系统门面（per-workspace 实例）。

    生命周期：每次 assemble 时按 workspace 创建（harness _build_memory_recall_middleware 注入）。
    持 workspace_id 定位 MemoryStore。

    Args:
        workspace_id: 作品隔离标识（定位 memory.db 文件）。
        retriever: 检索器（None 用默认单例，harness 可注入自定义）。
    """

    def __init__(self, workspace_id: str, *, retriever: MemoryRetriever | None = None) -> None:
        self._workspace_id = workspace_id
        self._retriever = retriever or get_memory_retriever()

    async def retrieve(
        self,
        query: str,
        group_id: str,  # 兼容旧签名（middleware 传），实际用 workspace_id
        *,
        causal_cutoff: int | None = None,
        budget_chars: int = 12000,
        num_results: int = 10,
    ) -> EvidencePacket:
        """四阶段检索（论文 §A.1）。

        签名与旧 Graphiti 版兼容（query + group_id + budget + num_results），
        新增 causal_cutoff kwarg（D-D4-1：向后兼容，旧调用不传则不过滤——不推荐）。

        流程：
          1. 取该作品的 MemoryStore（从 pool）
          2. embed query（智谱 embedding-3；失败则只走 BM25/LIKE 路）
          3. retriever.retrieve 执行四阶段

        失败语义：抛异常，由 middleware 捕获后降级（D-R5-1）。
        """
        pool = get_memory_store_pool()
        store = await pool.get(self._workspace_id)

        # embed query（向量路；embedder 不可用或失败时 query_embedding=None，只走 BM25/LIKE）
        query_embedding: list[float] | None = None
        embedder = get_memory_embedder()
        if embedder is not None:
            try:
                query_embedding = await embedder.embed_one(query)
            except MemoryEmbedError as e:
                logger.warning("query embed 失败，退化为纯 BM25/LIKE 检索：%s", e)
                query_embedding = None

        return await self._retriever.retrieve(
            store,
            query,
            group_id=group_id,
            query_embedding=query_embedding,
            causal_cutoff=causal_cutoff,
            budget_chars=budget_chars,
            num_results=num_results,
        )

    async def health_check(self) -> bool:
        """健康检查：SQLite 本地存储几乎不会挂，主要验证 store 可连接。

        复用旧接口名（harness middleware 调用）。SQLite 本地文件无网络往返，
        失败仅可能是文件损坏/磁盘满——这些场景下 retrieve 也会失败并降级。
        """
        try:
            pool = get_memory_store_pool()
            store = await pool.get(self._workspace_id)
            # 轻量探针：COUNT 一张表
            store.count_records("chapter_digest")
            return True
        except Exception as e:
            logger.warning("MemoryBackend 健康检查失败（workspace=%s）：%s", self._workspace_id, e)
            return False


__all__ = ["MemoryBackend", "EvidencePacket", "get_memory_backend", "reset_memory_backend"]


# ── 工厂函数（从 client.py 迁入，client.py 已删除）──────────────────

def get_memory_backend(workspace_id: str) -> "MemoryBackend | None":
    """为指定作品构建 MemoryBackend（per-workspace 实例）。

    去 Graphiti：不再需要 Graphiti 客户端/FalkorDB 连接。
    新 backend 是轻量门面，组合 MemoryStorePool + MemoryRetriever，按 workspace_id 定位 store。

    记忆系统是否"启用"的判断：
      - MemoryStorePool 必须已初始化（main.py lifespan 调 init_memory_store_pool）。
      - 否则返回 None（记忆功能关闭，writing 走 ContextAssembler 全量注入）。

    Args:
        workspace_id: 作品隔离标识（f"{owner_id}_{workspace_name}"，定位 memory.db）。

    Returns:
        MemoryBackend 实例；若 store pool 未初始化则 None（降级）。
    """
    try:
        from app.platform.memory.store import get_memory_store_pool

        # pool 未初始化（main.py 没调 init_memory_store_pool）→ 记忆关闭
        try:
            get_memory_store_pool()
        except RuntimeError:
            logger.info("MemoryStorePool 未初始化，记忆系统关闭，降级全量注入")
            return None

        return MemoryBackend(workspace_id)
    except Exception as e:
        logger.warning("MemoryBackend 构建失败，记忆系统关闭：%s", e, exc_info=True)
        return None


def reset_memory_backend() -> None:
    """重置记忆系统所有单例（仅用于测试）。

    backend 不再缓存单例（per-workspace 实例），但 embedder/extractor 是进程单例。
    此函数重置它们，方便测试隔离。
    """
    from app.platform.memory.embedder import reset_memory_embedder
    from app.platform.memory.extractor import reset_memory_extractor
    reset_memory_embedder()
    reset_memory_extractor()
