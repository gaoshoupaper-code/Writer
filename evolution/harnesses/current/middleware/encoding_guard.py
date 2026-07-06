"""EncodingGuardMiddleware — 文件编码自动检测与修复中间件。

职责：
  在 before_model hook 上拦截 read_file 工具调用，检测文件编码。
  若文件非 UTF-8 可解码，自动尝试用 latin-1 / cp1252 等常见编码回退读取
  并修复（写回 UTF-8）。解决文件编码损坏导致后续操作完全阻塞的问题。

使用方式：
  装配到 agent 的 before_model hook 处理器列表。
  fallback_encodings: 回退编码列表（按优先级）
  auto_fix: 是否自动修复并写回 UTF-8
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# 默认回退编码（按优先级）
_DEFAULT_FALLBACK_ENCODINGS = ["latin-1", "cp1252", "utf-16"]


class EncodingGuardMiddleware(AgentMiddleware):
    """文件编码自动检测与修复中间件。

    在 wrap_tool_call hook 上拦截 read_file 工具调用，检测文件编码。
    若文件非 UTF-8 可解码，自动尝试回退编码读取并修复。
    """

    def __init__(
        self,
        *,
        fallback_encodings: list[str] | None = None,
        auto_fix: bool = True,
    ) -> None:
        """
        Args:
            fallback_encodings: 回退编码列表（按优先级尝试），默认 latin-1, cp1252, utf-16
            auto_fix: 是否自动修复并写回 UTF-8，默认 True
        """
        self.fallback_encodings = fallback_encodings or _DEFAULT_FALLBACK_ENCODINGS
        self.auto_fix = auto_fix

    # ------------------------------------------------------------------
    # 工具调用拦截（同步 / 异步）
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """拦截同步工具调用：检测 read_file 的编码问题。"""
        if not self._is_read_file(request):
            return handler(request)

        file_path = self._get_file_path(request)
        if file_path is None:
            return handler(request)

        # 检查文件编码
        encoding_issue = self._detect_encoding_issue(file_path)
        if encoding_issue is None:
            return handler(request)

        # 有编码问题，尝试修复
        if self.auto_fix:
            fixed = self._fix_encoding(file_path, encoding_issue)
            if fixed:
                logger.info("Fixed encoding for %s: %s -> UTF-8", file_path, encoding_issue)
                return handler(request)

        # 无法修复，返回错误消息
        return self._encoding_error_message(request, file_path, encoding_issue)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """拦截异步工具调用：检测 read_file 的编码问题。"""
        if not self._is_read_file(request):
            return await handler(request)

        file_path = self._get_file_path(request)
        if file_path is None:
            return await handler(request)

        # 检查文件编码
        encoding_issue = self._detect_encoding_issue(file_path)
        if encoding_issue is None:
            return await handler(request)

        # 有编码问题，尝试修复
        if self.auto_fix:
            fixed = self._fix_encoding(file_path, encoding_issue)
            if fixed:
                logger.info("Fixed encoding for %s: %s -> UTF-8", file_path, encoding_issue)
                return await handler(request)

        # 无法修复，返回错误消息
        return self._encoding_error_message(request, file_path, encoding_issue)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _is_read_file(self, request: Any) -> bool:
        """判断是否为 read_file 工具调用。"""
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        return str(tool_name) == "read_file"

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
        for enc in self.fallback_encodings:
            try:
                raw_bytes.decode(enc)
                return enc  # 找到可解码的编码
            except (UnicodeDecodeError, LookupError):
                continue

        return "unknown"  # 所有编码都失败

    def _fix_encoding(self, file_path: Path, source_encoding: str) -> bool:
        """修复文件编码：从 source_encoding 转码为 UTF-8 写回。

        Returns:
            True 表示修复成功，False 表示失败。
        """
        try:
            raw_bytes = file_path.read_bytes()
            if source_encoding == "unknown":
                # 尝试强制用 latin-1（不会失败，但可能产生乱码）
                text = raw_bytes.decode("latin-1")
            else:
                text = raw_bytes.decode(source_encoding)
            # 写回 UTF-8
            file_path.write_text(text, encoding="utf-8")
            return True
        except Exception as exc:
            logger.error("Failed to fix encoding for %s: %s", file_path, exc)
            return False

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


__all__ = ["EncodingGuardMiddleware"]
