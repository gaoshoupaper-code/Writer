"""记忆系统固定基础设施（executor/platform/memory）。

提供 MemoryBackend 抽象——封装 Graphiti + FalkorDB + LLM + embedder，
对 harness middleware 隐藏图库细节。

模块职责：
  backend.py    — MemoryBackend 核心（四原语：retrieve/add_episode/ingest/health_check）
  client.py     — Graphiti 客户端工厂（进程单例，懒加载）
  ingestion.py  — 异步入图管道（storybuilding + chapter 两个触发点）
  errors.py     — MemoryUnavailableError（决策 10 失败语义）

可进化要素在 harness 包内（evolution/harnesses/repo/）：
  middleware/memory_recall_middleware.py — 查询构造 + 证据包组装
  tools/narrative_schema.py             — 实体/边类型定义
  tools/storyline_parser.py             — storybuilding 结构化解析
  tools/story_calendar.py               — 虚构历法映射

设计文档：.claude/md/20260714_110000_记忆系统改造设计.md
"""
from app.platform.memory.backend import EvidencePacket, MemoryBackend
from app.platform.memory.client import get_memory_backend, reset_memory_backend
from app.platform.memory.errors import MemoryUnavailableError
from app.platform.memory.ingestion import (
    clear_unhealthy,
    ingest_chapter_sync,
    ingest_storybuilding_sync,
    is_unhealthy,
)

__all__ = [
    "MemoryBackend",
    "EvidencePacket",
    "MemoryUnavailableError",
    "get_memory_backend",
    "reset_memory_backend",
    "ingest_storybuilding_sync",
    "ingest_chapter_sync",
    "is_unhealthy",
    "clear_unhealthy",
]
