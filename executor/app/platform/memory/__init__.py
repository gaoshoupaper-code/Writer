"""NWM 记忆系统固定基础设施（executor/platform/memory）。

去 Graphiti 重构（2026-07-17）：自研叙事学类型化记忆，存储用 SQLite + sqlite-vec + FTS5。

模块职责：
  store.py      — MemoryStore（SQLite 存取 + DDL）+ MemoryStorePool（LRU 连接池）
  embedder.py   — 智谱 embedding-3 客户端（向量生成）
  extractor.py  — LLM 抽取（章节正文 → 8 类 typed records，json_object 降级）
  ingestion.py  — extract_and_publish 管道（extract → embed → store，含 PlotPromise 状态机）
  retriever.py  — 四阶段检索编排（causal cutoff + FTS5/vec RRF + one-hop JOIN + bounded packet）
  backend.py    — MemoryBackend 门面 + get_memory_backend 工厂（Phase 6：client.py 已删，工厂迁入）

可进化要素在 harness 包（evolution/harnesses/repo/）：
  middleware/memory_recall_middleware.py — 查询构造 + 证据包排版 + 失败降级
  tools/narrative_schema.py             — 8 类 record 元信息 + 题材策略
  tools/query_builder.py                — task→writer query 构造
  tools/join_rules.py                   — one-hop JOIN 扩展规则
  tools/packet_formatter.py             — 证据包排版
  prompts/memory_extraction_guide.md    — 抽取 prompt（可覆盖 executor 默认）

设计文档：.claude/md/20260717_192700_NWM记忆系统设计.md
"""
# 存取与抽取（Phase 1-3）
from app.platform.memory.embedder import MemoryEmbedder, get_memory_embedder
from app.platform.memory.extractor import ChapterRecords, MemoryExtractor, get_memory_extractor
from app.platform.memory.ingestion import (
    build_search_text,
    clear_unhealthy,
    extract_and_publish,
    extract_and_publish_sync,
    is_unhealthy,
)
from app.platform.memory.retriever import (
    EvidencePacket,
    MemoryRetriever,
    get_memory_retriever,
    set_memory_retriever,
)
from app.platform.memory.store import MemoryStore, get_memory_store_pool

# 门面（Phase 4：薄门面，组合 store+embedder+retriever；Phase 6：client.py 已删，工厂函数迁入）
from app.platform.memory.backend import MemoryBackend, get_memory_backend, reset_memory_backend

__all__ = [
    # 门面 + 工厂
    "MemoryBackend",
    "EvidencePacket",
    "get_memory_backend",
    "reset_memory_backend",
    # 存储
    "MemoryStore",
    "get_memory_store_pool",
    # embedding
    "MemoryEmbedder",
    "get_memory_embedder",
    # 抽取
    "MemoryExtractor",
    "ChapterRecords",
    "get_memory_extractor",
    # 检索
    "MemoryRetriever",
    "get_memory_retriever",
    "set_memory_retriever",
    # 管道
    "extract_and_publish",
    "extract_and_publish_sync",
    "build_search_text",
    # 健康标记
    "is_unhealthy",
    "clear_unhealthy",
]
