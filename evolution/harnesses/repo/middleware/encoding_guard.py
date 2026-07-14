"""EncodingGuardMiddleware — 文件编码自动检测与修复中间件。

职责：
  在 wrap_tool_call hook 上拦截 read_file / write_file 工具调用。
  - write_file 完成后：校验文件编码合法性，若编码损坏则拒绝写入并报错。
  - read_file 前：检测编码问题，自动尝试降级解码。

使用方式：
  装配到 agent 的 wrap_tool_call hook 处理器列表。
  allowed_encodings: 允许的编码列表（默认 ['utf-8', 'utf-8-sig']）
  repair_on_read: 读取时若检测到非 UTF-8，尝试降级读取
  strict_write: 写入后校验，若编码损坏则拒绝写入并报错
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# 默认允许的编码
_DEFAULT_ALLOWED_ENCODINGS = ["utf-8", "utf-8-sig"]

# 读取降级编码（按优先级）
_READ_FALLBACK_ENCODINGS = ["utf-8-sig", "gbk", "latin-1"]


class EncodingError(Exception):
    """文件编码错误异常。"""
    pass


class EncodingGuardMiddleware(AgentMiddleware):
    """文件编码自动检测与修复中间件。

    在 wrap_tool_call hook 上拦截 read_file / write_file 工具调用，
    检测文件编码问题并提供降级或拦截。
    """

    def __init__(
        self,
        *,
        allowed_encodings: list[str] | None = None,
        repair_on_read: bool = True,
        strict_write: bool = True,
    ) -> None:
        """
        Args:
            allowed_encodings: 允许的编码列表，默认 ['utf-8', 'utf-8-sig']
            repair_on_read: 读取时若检测到非 UTF-8，尝试降级读取，默认 True
            strict_write: 写入后校验，若编码损坏则拒绝写入并报错，默认 True
        """
        self.allowed_encodings = allowed_encodings or _DEFAULT_ALLOWED_ENCODINGS
        self.repair_on_read = repair_on_read
        self.strict_write = strict_write

    # ------------------------------------------------------------------
    # 工具调用拦截（同步）
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """拦截同步工具调用：检测 read_file / write_file 的编码问题。"""
        tool_name = self._get_tool_name(request)

        if tool_name == "read_file":
            return self._handle_read_file(request, handler)
        elif tool_name == "write_file" and self.strict_write:
            return self._handle_write_file(request, handler)

        return handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """拦截异步工具调用：检测 read_file / write_file 的编码问题。"""
        tool_name = self._get_tool_name(request)

        if tool_name == "read_file":
            return await self._ahandle_read_file(request, handler)
        elif tool_name == "write_file" and self.strict_write:
            return await self._ahandle_write_file(request, handler)

        return await handler(request)

        # 无法修复，返回错误消息
        return self._encoding_error_message(request, file_path, encoding_issue)

    # ------------------------------------------------------------------
    # read_file 处理
    # ------------------------------------------------------------------

    def _handle_read_file(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """同步：处理 read_file 调用，检测编码问题并提供降级。"""
        file_path = self._get_file_path(request)
        if file_path is None:
            return handler(request)

        # 检查文件编码
        encoding_issue = self._detect_encoding_issue(file_path)
        if encoding_issue is None:
            return handler(request)

        # 有编码问题，尝试降级读取
        if self.repair_on_read:
            content = self._read_with_fallback(file_path)
            if content is not None:
                logger.warning(
                    "Read file with fallback encoding: %s (original: %s)",
                    file_path, encoding_issue,
                )
                return self._make_read_file_response(request, content)

        # 无法降级，返回错误消息
        return self._encoding_error_message(request, file_path, encoding_issue)

    async def _ahandle_read_file(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """异步：处理 read_file 调用，检测编码问题并提供降级。"""
        file_path = self._get_file_path(request)
        if file_path is None:
            return await handler(request)

        # 检查文件编码
        encoding_issue = self._detect_encoding_issue(file_path)
        if encoding_issue is None:
            return await handler(request)

        # 有编码问题，尝试降级读取
        if self.repair_on_read:
            content = self._read_with_fallback(file_path)
            if content is not None:
                logger.warning(
                    "Read file with fallback encoding: %s (original: %s)",
                    file_path, encoding_issue,
                )
                return self._make_read_file_response(request, content)

        # 无法降级，返回错误消息
        return self._encoding_error_message(request, file_path, encoding_issue)

    # ------------------------------------------------------------------
    # write_file 处理
    # ------------------------------------------------------------------

    def _handle_write_file(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """同步：处理 write_file 调用，写入后校验编码。"""
        result = handler(request)
        self._validate_written_file(request)
        return result

    async def _ahandle_write_file(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """异步：处理 write_file 调用，写入后校验编码。"""
        result = await handler(request)
        self._validate_written_file(request)
        return result

    def _validate_written_file(self, request: Any) -> None:
        """写入后校验文件编码，若损坏则删除文件并抛出 EncodingError。"""
        file_path = self._get_file_path(request)
        if file_path is None:
            return

        if not file_path.exists():
            return

        raw_bytes = file_path.read_bytes()
        if not raw_bytes:
            return

        # 用 allowed_encodings 逐一尝试解码
        for enc in self.allowed_encodings:
            try:
                raw_bytes.decode(enc)
                return  # 至少一种编码可解码，校验通过
            except (UnicodeDecodeError, LookupError):
                continue

        # 所有编码都失败，删除文件并报错
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass

        raise EncodingError(
            f"文件编码校验失败：{file_path} 无法以 {self.allowed_encodings} 解码。"
            "文件已被删除，请检查内容编码后重试。"
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _get_tool_name(self, request: Any) -> str | None:
        """从工具调用中提取工具名。"""
        tool_call = getattr(request, "tool_call", {})
        return str(_mapping_value(tool_call, "name") or "")

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

    def _detect_encoding_issue(self, file_path: Path) -> str | None:
        """检测文件编码问题。

        尝试以 UTF-8 读取文件。若失败，尝试回退编码。
        返回检测到的实际编码，或 None（无问题）。
        """
        if not file_path.exists():
            return None

        raw_bytes = file_path.read_bytes()
        if not raw_bytes:
            return None

        # 尝试 UTF-8
        try:
            raw_bytes.decode("utf-8")
            return None  # UTF-8 可解码，无问题
        except UnicodeDecodeError:
            pass

        # 尝试回退编码
        for enc in _READ_FALLBACK_ENCODINGS:
            try:
                raw_bytes.decode(enc)
                return enc  # 找到可解码的编码
            except (UnicodeDecodeError, LookupError):
                continue

        return "unknown"  # 所有编码都失败

    def _read_with_fallback(self, file_path: Path) -> str | None:
        """用降级编码读取文件内容。

        Returns:
            解码后的文本，或 None（所有编码都失败）。
        """
        raw_bytes = file_path.read_bytes()
        if not raw_bytes:
            return ""

        for enc in _READ_FALLBACK_ENCODINGS:
            try:
                return raw_bytes.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue

        return None

    def _make_read_file_response(self, request: Any, content: str) -> ToolMessage:
        """构造降级读取成功的响应消息。"""
        tool_call = getattr(request, "tool_call", {})
        tool_call_id = _mapping_value(tool_call, "id")
        return ToolMessage(
            content=content,
            name="read_file",
            tool_call_id=str(tool_call_id or ""),
        )

    def _encoding_error_message(self, request: Any, file_path: Path, encoding: str) -> ToolMessage:
        """构造编码错误消息。"""
        tool_call = getattr(request, "tool_call", {})
        tool_call_id = _mapping_value(tool_call, "id")
        return ToolMessage(
            content=(
                f"文件编码错误：{file_path} 无法以 UTF-8 解码"
                f"（检测到编码：{encoding}，自动修复失败）。"
                "请检查文件编码或手动转换为 UTF-8 后重试。"
            ),
            name="read_file",
            tool_call_id=str(tool_call_id or ""),
        )


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


__all__ = ["EncodingGuardMiddleware", "EncodingError"]
