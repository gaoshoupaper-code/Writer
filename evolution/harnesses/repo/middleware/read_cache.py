"""ReadCacheMiddleware — 文件读取缓存中间件（A2 D2 加固版）。

职责：
  在 wrap_tool_call hook 上拦截 read_file 工具调用，对读取结果进行
  内容哈希缓存。同一文件在同一 agent 生命周期内重复读取时直接返回缓存，
  减少冗余文件读取和 token 消耗。

A2 D2 关键加固：写后失效钩子
  原 ReadCache 只拦 read_file，对 write_file/edit_file 完全放行——文件被
  修改后 TTL 内仍返回旧内容，会放大 edit 闭环失败（storybuilding 已踩过：
  第 1 轮 read 缓存 → 第 2 轮 edit 修改 → 第 3 轮 read 命中旧缓存 →
  LLM 基于旧内容做下一步 edit → old_string 不匹配 → 连环失败）。

  A2 修复：拦 write_file/edit_file，执行后清除对应 file_path 的缓存 key，
  下一次 read 必然从磁盘重读最新内容。

使用方式：
  装配到 agent 的 wrap_tool_call hook 处理器列表。
  ttl_seconds: 缓存有效期（默认 300 秒）
  max_cache_size: 最大缓存文件数（默认 50）
  track_stats: 是否记录缓存命中/未命中统计（默认 True）

设计依据：.claude/md/20260720_150000_trace交付物丢失与基础设施归因.md §D2
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


class _CacheEntry:
    """缓存条目。"""

    def __init__(self, content: str, ttl_seconds: int) -> None:
        self.content = content
        self.expires_at = time.monotonic() + ttl_seconds

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


class ReadCacheMiddleware(AgentMiddleware):
    """文件读取缓存中间件。

    在 wrap_tool_call hook 上拦截 read_file 工具调用，对读取结果进行
    内容哈希缓存。同一文件在同一 agent 生命周期内重复读取时直接返回缓存。
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_cache_size: int = 50,
        track_stats: bool = True,
    ) -> None:
        """
        Args:
            ttl_seconds: 缓存有效期（秒），默认 300（5 分钟）
            max_cache_size: 最大缓存文件数，默认 50
            track_stats: 是否记录缓存命中/未命中统计，默认 True
        """
        self.ttl_seconds = ttl_seconds
        self.max_cache_size = max_cache_size
        self.track_stats = track_stats

        # 文件路径 → _CacheEntry
        self._cache: dict[str, _CacheEntry] = {}
        # 统计
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # 工具调用拦截（同步 / 异步）
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """拦截同步工具调用：缓存 read_file，写后失效 write_file/edit_file。"""
        tool_kind = self._classify_tool(request)

        # write_file / edit_file：执行后清除对应 file_path 的缓存（D2 写后失效）
        if tool_kind == "write":
            result = handler(request)
            self._invalidate_for_request(request)
            return result

        # 非 read_file 工具：完全透传
        if tool_kind != "read":
            return handler(request)

        file_path = self._get_file_path(request)
        if file_path is None:
            return handler(request)

        # 检查缓存
        cached = self._get_cached(file_path)
        if cached is not None:
            if self.track_stats:
                self._hits += 1
                logger.debug("ReadCache HIT: %s (hits=%d, misses=%d)", file_path, self._hits, self._misses)
            return self._make_cached_response(request, cached)

        # 缓存未命中，调用内层 handler
        if self.track_stats:
            self._misses += 1
            logger.debug("ReadCache MISS: %s (hits=%d, misses=%d)", file_path, self._hits, self._misses)

        result = handler(request)

        # 缓存结果
        self._set_cached(file_path, result)

        return result

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """拦截异步工具调用：缓存 read_file，写后失效 write_file/edit_file。"""
        tool_kind = self._classify_tool(request)

        if tool_kind == "write":
            result = await handler(request)
            self._invalidate_for_request(request)
            return result

        if tool_kind != "read":
            return await handler(request)

        file_path = self._get_file_path(request)
        if file_path is None:
            return await handler(request)

        # 检查缓存
        cached = self._get_cached(file_path)
        if cached is not None:
            if self.track_stats:
                self._hits += 1
                logger.debug("ReadCache HIT: %s (hits=%d, misses=%d)", file_path, self._hits, self._misses)
            return self._make_cached_response(request, cached)

        # 缓存未命中，调用内层 handler
        if self.track_stats:
            self._misses += 1
            logger.debug("ReadCache MISS: %s (hits=%d, misses=%d)", file_path, self._hits, self._misses)

        result = await handler(request)

        # 缓存结果
        self._set_cached(file_path, result)

        return result

    # ------------------------------------------------------------------
    # 缓存管理
    # ------------------------------------------------------------------

    def _get_cached(self, file_path: Path) -> str | None:
        """获取缓存内容。若不存在或已过期，返回 None。"""
        key = str(file_path)
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.is_expired:
            del self._cache[key]
            return None
        return entry.content

    def _set_cached(self, file_path: Path, result: Any) -> None:
        """缓存读取结果。

        从 result 中提取文本内容并缓存。
        """
        key = str(file_path)

        # 从 ToolMessage 或字符串中提取内容
        content = self._extract_content(result)
        if content is None:
            return  # 不缓存非文本结果

        # 缓存淘汰：超过 max_cache_size 时删除最旧的条目
        if len(self._cache) >= self.max_cache_size:
            self._evict_oldest()

        self._cache[key] = _CacheEntry(content, self.ttl_seconds)

    def _evict_oldest(self) -> None:
        """淘汰最旧的缓存条目。"""
        if not self._cache:
            return
        oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k].expires_at)
        del self._cache[oldest_key]

    def _extract_content(self, result: Any) -> str | None:
        """从工具调用结果中提取文本内容。"""
        if isinstance(result, str):
            return result
        if isinstance(result, ToolMessage):
            content = result.content
            if isinstance(content, str):
                return content
        return None

    def _make_cached_response(self, request: Any, content: str) -> ToolMessage:
        """构造缓存命中的响应消息。"""
        tool_call = getattr(request, "tool_call", {})
        tool_call_id = _mapping_value(tool_call, "id")
        return ToolMessage(
            content=content,
            name="read_file",
            tool_call_id=str(tool_call_id or ""),
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _is_read_file(self, request: Any) -> bool:
        """判断是否为 read_file 工具调用。"""
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        return str(tool_name) == "read_file"

    def _classify_tool(self, request: Any) -> str:
        """分类工具调用：'read' / 'write' / 'other'。

        A2 D2：write_file / edit_file 都归类为 'write'，触发写后失效。
        """
        tool_call = getattr(request, "tool_call", {})
        tool_name = str(_mapping_value(tool_call, "name") or "")
        if tool_name == "read_file":
            return "read"
        if tool_name in ("write_file", "edit_file"):
            return "write"
        return "other"

    def _invalidate_for_request(self, request: Any) -> None:
        """写后失效：清除请求对应 file_path 的缓存 key。

        A2 D2：write_file/edit_file 执行后调用，下一次 read 必然从磁盘重读
        最新内容，避免 TTL 内返回旧内容放大 edit 闭环失败。
        """
        file_path = self._get_file_path(request)
        if file_path is None:
            return
        key = str(file_path)
        if key in self._cache:
            del self._cache[key]
            logger.debug("ReadCache INVALIDATE on write: %s", file_path)

    def _get_file_path(self, request: Any) -> Path | None:
        """从工具调用中提取文件路径。"""
        tool_call = getattr(request, "tool_call", {})
        args = _mapping_value(tool_call, "args")
        if not isinstance(args, dict):
            return None
        path_str = args.get("file_path") or args.get("path") or ""
        if not isinstance(path_str, str) or not path_str:
            return None
        return Path(path_str)

    # ------------------------------------------------------------------
    # 统计信息
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict[str, int]:
        """返回缓存命中/未命中统计。"""
        return {"hits": self._hits, "misses": self._misses}


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


__all__ = ["ReadCacheMiddleware"]
