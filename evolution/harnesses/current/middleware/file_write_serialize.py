"""FileWriteSerializeMiddleware — 文件写操作串行化中间件。

职责：
  按 file_path 串行化 write_file / edit_file 调用，防止并发写同一文件
  竞争导致文件字节流损坏（UTF-8 多字节字符被截断，产生非法字节）。

  当 allow_overwrite=True 时，允许 write_file 覆盖已存在的文件，
  跳过文件已存在检查，直接调用内层 handler。

根因背景：
  langgraph ToolNode 用 ``asyncio.gather`` 并发执行同一轮模型输出的多个
  tool_call（见 langgraph/prebuilt/tool_node.py）。当模型一次发起多个针对
  *同一文件* 的 edit_file 时，并发执行的 ``os.open(O_TRUNC)`` + UTF-8 写回
  （见 deepagents/backends/filesystem.py:edit）会交叉：一个写把另一个的
  字节流拦腰截断，留下孤立的 UTF-8 continuation byte（如 0x8c），下次读取
  即报 ``'utf-8' codec can't decode``，表现为「编码损坏」。

  deepagents 的 read/write/edit 本身全程硬编码 UTF-8，编码处理无 bug；
  真正的病根是并发竞争，故按文件加锁串行化写操作即可根治。

  只串行化写操作；read 等读操作不加锁，保留读并发。同文件串行、不同文件
  仍并发，开销最小。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

logger = logging.getLogger(__name__)

# 需要按文件串行化的写工具：工具名 → 路径参数名（兼容 file_path / path 两种命名）
_WRITE_TOOLS: dict[str, tuple[str, ...]] = {
    "write_file": ("file_path", "path"),
    "edit_file": ("file_path", "path"),
}


class FileWriteSerializeMiddleware(AgentMiddleware):
    """按 file_path 串行化文件写工具，防止并发竞争损坏文件。

    锁为 middleware 实例级。同一 agent graph 内并发的多个 tool_call
    共享同一实例，因此能串行化「同一轮里多个针对同一文件的 edit/write」。
    asyncio 单线程模型下 ``setdefault`` 之间无 await，锁的 lazy 创建是
    原子的，不会产生竞争态。

    allow_overwrite=True 时，允许 write_file 覆盖已存在的文件，
    跳过文件已存在检查，直接调用内层 handler。
    """

    def __init__(self, *, allow_overwrite: bool = False) -> None:
        """
        Args:
            allow_overwrite: 是否允许 write_file 覆盖已存在的文件，默认 False
        """
        self.allow_overwrite = allow_overwrite
        # file_path → 锁；lazy 创建，见 _lock_for。
        self._locks: dict[str, asyncio.Lock] = {}

    def _write_file_key(self, request: Any) -> str | None:
        """若为写工具调用，返回其目标 file_path；否则返回 None（不串行化）。"""
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        path_fields = _WRITE_TOOLS.get(str(tool_name))
        if path_fields is None:
            return None
        args = _mapping_value(tool_call, "args")
        if not isinstance(args, dict):
            return None
        for field in path_fields:
            path = args.get(field)
            if isinstance(path, str) and path:
                return path
        return None

    def _lock_for(self, key: str) -> asyncio.Lock:
        """获取（必要时创建）指定 file_path 的锁。"""
        return self._locks.setdefault(key, asyncio.Lock())

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """异步：按 file_path 加锁串行化写工具；其余直接放行。"""
        key = self._write_file_key(request)
        if key is None:
            return await handler(request)
        if self.allow_overwrite:
            logger.warning("write_file overwrite allowed for %s", key)
        async with self._lock_for(key):
            return await handler(request)

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """同步：langgraph 同步 ToolNode 顺序执行多 tool_call，无并发竞争，直接放行。"""
        key = self._write_file_key(request)
        if key is not None and self.allow_overwrite:
            logger.warning("write_file overwrite allowed for %s", key)
        return handler(request)


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)
