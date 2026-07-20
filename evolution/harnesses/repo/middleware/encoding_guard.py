"""EncodingGuardMiddleware — 文件编码 + 写入完整性校验中间件（D1 重构版）。

职责（A2 重构后）：
  在 wrap_tool_call hook 上拦截 read_file / write_file 工具调用。
  - write_file 完成后：
      (a) 编码合法性校验：写入的字节流必须是合法 UTF-8/UTF-8-SIG。
      (b) 内容完整性校验（A2 新增）：args.content 的长度 vs 磁盘回读字节数
          不一致 → 检测出"写入层截断"，拒绝这次写入。
      损坏时删除文件 + 返回 error ToolMessage（不抛异常，让 LLM 看到清晰
      错误后换内容/路径重写，不让 ErrorRecovery 无脑重试同样的内容——R1 修复）。
  - read_file 前：UTF-8 解码失败时降级到 utf-8-sig（仅此一种）。
      其他编码（gbk/latin-1）不再降级——latin-1 会把任意字节流"洗白"成乱码
      正常返回（A2 决策：彻底去除洗白风险）。

A2 关键修复：
  1. 加截断检测：args.content 长度 vs 磁盘字节数比对（仅防"写入层截断"，
     不防 LLM 自身 token 截断——后者属 B/C 范畴）。
  2. 去 latin-1 洗白：read 降级链只保留 utf-8-sig。
  3. 不抛异常改返回 error ToolMessage：避免 ErrorRecovery 重试 unlink 后的
     失效状态（R1 修复）。
  4. 清理死代码（原 awrap_tool_call 后的不可达语句已删）。

设计依据：.claude/md/20260720_150000_trace交付物丢失与基础设施归因.md §D1
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# 默认允许的编码（写入后校验）
_DEFAULT_ALLOWED_ENCODINGS = ["utf-8", "utf-8-sig"]

# 读取降级编码（A2 重构：只保留 utf-8-sig，移除 gbk/latin-1 防洗白）
_READ_FALLBACK_ENCODINGS = ["utf-8-sig"]

# 截断检测容差：args.content 编码后字节数与磁盘字节数允许的差异（字节）。
# 设小容差是为容忍末尾换行处理等微小差异；不容忍任何实质性截断。
_TRUNCATION_TOLERANCE_BYTES = 4


class EncodingGuardMiddleware(AgentMiddleware):
    """文件编码 + 写入完整性校验中间件（A2 重构版）。

    在 wrap_tool_call hook 上拦截 read_file / write_file 工具调用，
    检测编码问题并提供降级，或检测到写入层截断时拒绝这次写入。
    """

    def __init__(
        self,
        *,
        allowed_encodings: list[str] | None = None,
        repair_on_read: bool = True,
        strict_write: bool = True,
        check_truncation: bool = True,
    ) -> None:
        """
        Args:
            allowed_encodings: 允许的编码列表，默认 ['utf-8', 'utf-8-sig']
            repair_on_read: 读取时若检测到非 UTF-8，尝试降级到 utf-8-sig，默认 True
            strict_write: 写入后校验编码合法性 + 完整性，默认 True
            check_truncation: 写入后校验内容完整性（A2 新增），默认 True
        """
        self.allowed_encodings = allowed_encodings or _DEFAULT_ALLOWED_ENCODINGS
        self.repair_on_read = repair_on_read
        self.strict_write = strict_write
        self.check_truncation = check_truncation

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

    # ------------------------------------------------------------------
    # read_file 处理
    # ------------------------------------------------------------------

    def _handle_read_file(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """同步：处理 read_file 调用，检测编码问题并提供降级。"""
        file_path = self._get_file_path(request)
        if file_path is None:
            return handler(request)

        encoding_issue = self._detect_encoding_issue(file_path)
        if encoding_issue is None:
            return handler(request)

        # 有编码问题，尝试 utf-8-sig 降级
        if self.repair_on_read:
            content = self._read_with_fallback(file_path)
            if content is not None:
                logger.warning(
                    "Read file with fallback encoding: %s (original issue: %s)",
                    file_path, encoding_issue,
                )
                return self._make_read_file_response(request, content)

        # 无法降级，返回错误消息（不抛异常）
        return self._encoding_error_message(request, file_path, encoding_issue)

    async def _ahandle_read_file(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """异步：处理 read_file 调用，检测编码问题并提供降级。"""
        file_path = self._get_file_path(request)
        if file_path is None:
            return await handler(request)

        encoding_issue = self._detect_encoding_issue(file_path)
        if encoding_issue is None:
            return await handler(request)

        if self.repair_on_read:
            content = self._read_with_fallback(file_path)
            if content is not None:
                logger.warning(
                    "Read file with fallback encoding: %s (original issue: %s)",
                    file_path, encoding_issue,
                )
                return self._make_read_file_response(request, content)

        return self._encoding_error_message(request, file_path, encoding_issue)

    # ------------------------------------------------------------------
    # write_file 处理
    # ------------------------------------------------------------------

    def _handle_write_file(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """同步：处理 write_file 调用，写入后校验编码 + 完整性。

        校验失败 → 删除文件 + 返回 error ToolMessage（不抛异常，让 LLM 看到
        清晰错误换内容重写，不让 ErrorRecovery 无脑重试——R1 修复）。
        """
        result = handler(request)
        error_msg = self._validate_written_file(request)
        if error_msg is not None:
            return self._make_write_error_response(request, error_msg)
        return result

    async def _ahandle_write_file(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """异步：处理 write_file 调用，写入后校验编码 + 完整性。"""
        result = await handler(request)
        error_msg = self._validate_written_file(request)
        if error_msg is not None:
            return self._make_write_error_response(request, error_msg)
        return result

    def _validate_written_file(self, request: Any) -> str | None:
        """写入后校验文件编码 + 完整性。

        Returns:
            None = 校验通过；str = 失败原因（调用方据此返回 error ToolMessage）。
            失败时文件已被 unlink（避免下游消费损坏/截断文件）。
        """
        file_path = self._get_file_path(request)
        if file_path is None or not file_path.exists():
            return None

        raw_bytes = file_path.read_bytes()
        if not raw_bytes:
            return None  # 空文件视为合法（LLM 可能确实写了空内容）

        # ── (a) 编码合法性校验 ──
        decoded_ok = False
        for enc in self.allowed_encodings:
            try:
                raw_bytes.decode(enc)
                decoded_ok = True
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if not decoded_ok:
            self._safe_unlink(file_path)
            return (
                f"文件编码校验失败：{file_path} 无法以 {self.allowed_encodings} 解码。"
                "文件已被删除，请检查内容编码后重试。"
            )

        # ── (b) 内容完整性校验（A2 新增：防写入层截断）──
        if self.check_truncation:
            expected_bytes = self._expected_content_bytes(request)
            if expected_bytes is not None:
                actual_bytes = len(raw_bytes)
                diff = expected_bytes - actual_bytes
                if diff > _TRUNCATION_TOLERANCE_BYTES:
                    # 写入层截断：磁盘字节数明显少于 args.content 编码后字节数
                    self._safe_unlink(file_path)
                    return (
                        f"文件完整性校验失败：{file_path} 疑似被截断。"
                        f"预期 {expected_bytes} 字节，实际 {actual_bytes} 字节"
                        f"（差 {diff} 字节）。文件已被删除，请重新写入完整内容。"
                    )

        return None

    def _expected_content_bytes(self, request: Any) -> int | None:
        """从 write_file 请求的 args.content 算出预期字节数。

        deepagents write_file 的 args.content 就是纯正文（StructuredTool schema
        只有 file_path + content 两个字段，无 thinking 等元数据），所以
        args.content 编码后字节数 = 应写入磁盘的字节数。

        Returns:
            预期字节数，或 None（无法提取 args.content 时跳过完整性校验）。
        """
        tool_call = getattr(request, "tool_call", {})
        args = _mapping_value(tool_call, "args")
        if not isinstance(args, dict):
            return None
        content = args.get("content")
        if not isinstance(content, str) or not content:
            return None
        return len(content.encode("utf-8"))

    def _safe_unlink(self, file_path: Path) -> None:
        """安全删除文件（失败不抛异常，仅记录日志）。"""
        try:
            file_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed to unlink corrupted file %s: %s", file_path, exc)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _get_tool_name(self, request: Any) -> str | None:
        tool_call = getattr(request, "tool_call", {})
        return str(_mapping_value(tool_call, "name") or "")

    def _get_file_path(self, request: Any) -> Path | None:
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

        尝试以 UTF-8 读取文件。若失败，尝试 utf-8-sig 回退编码。
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
            return None
        except UnicodeDecodeError:
            pass

        # 尝试 utf-8-sig 回退
        for enc in _READ_FALLBACK_ENCODINGS:
            try:
                raw_bytes.decode(enc)
                return enc
            except (UnicodeDecodeError, LookupError):
                continue

        return "unknown"

    def _read_with_fallback(self, file_path: Path) -> str | None:
        """用降级编码读取文件内容（A2 重构：只保留 utf-8-sig）。

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
        tool_call = getattr(request, "tool_call", {})
        tool_call_id = _mapping_value(tool_call, "id")
        return ToolMessage(
            content=content,
            name="read_file",
            tool_call_id=str(tool_call_id or ""),
        )

    def _encoding_error_message(
        self, request: Any, file_path: Path, encoding: str
    ) -> ToolMessage:
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
            status="error",
        )

    def _make_write_error_response(
        self, request: Any, error_msg: str
    ) -> ToolMessage:
        """构造 write_file 校验失败的 error ToolMessage。

        不抛异常 → ErrorRecovery 不介入 → LLM 直接看到清晰错误，
        根据错误内容调整后重写（避免无脑重试同样的损坏内容——R1 修复）。
        """
        tool_call = getattr(request, "tool_call", {})
        tool_call_id = _mapping_value(tool_call, "id")
        return ToolMessage(
            content=error_msg,
            name="write_file",
            tool_call_id=str(tool_call_id or ""),
            status="error",
        )


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


__all__ = ["EncodingGuardMiddleware"]
