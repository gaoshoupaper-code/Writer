"""FileWriteGuardMiddleware — 文件写入冲突自动处理中间件。

职责：
  在 wrap_tool_call hook 上拦截 write_file 工具调用，当目标文件已存在时，
  自动追加时间戳后缀（如 /review/storybuilding.md → /review/storybuilding_20260706_091500.md）
  而非报错。解决 review 子代理反复遭遇 write_file 已存在错误的问题。

使用方式：
  装配到 agent 的 wrap_tool_call hook 处理器列表。
  conflict_strategy: 冲突处理策略（timestamp_suffix / overwrite / skip）
  suffix_format: 时间戳后缀格式（strftime 格式）
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# 需要拦截的文件写入工具名称
_WRITE_TOOLS = {"write_file"}

# 默认时间戳格式
_DEFAULT_SUFFIX_FORMAT = "_%Y%m%d_%H%M%S"


class FileWriteGuardMiddleware(AgentMiddleware):
    """文件写入冲突自动处理中间件。

    拦截 write_file 工具调用，当目标文件已存在时自动追加时间戳后缀，
    避免因文件已存在导致的写入失败。
    """

    def __init__(
        self,
        *,
        conflict_strategy: str = "timestamp_suffix",
        suffix_format: str = _DEFAULT_SUFFIX_FORMAT,
    ) -> None:
        """
        Args:
            conflict_strategy: 冲突处理策略
                - "timestamp_suffix": 追加时间戳后缀（默认）
                - "overwrite": 直接覆盖已存在文件
                - "skip": 跳过写入，返回已存在提示
            suffix_format: 时间戳后缀格式（strftime 格式），默认 _%Y%m%d_%H%M%S
        """
        if conflict_strategy not in ("timestamp_suffix", "overwrite", "skip"):
            raise ValueError(f"Unknown conflict_strategy: {conflict_strategy}")
        self.conflict_strategy = conflict_strategy
        self.suffix_format = suffix_format

    # ------------------------------------------------------------------
    # 工具调用拦截（同步 / 异步）
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """拦截同步工具调用：处理 write_file 冲突。"""
        modified = self._handle_conflict(request)
        if modified is not None:
            return modified
        return handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """拦截异步工具调用：处理 write_file 冲突。"""
        modified = self._handle_conflict(request)
        if modified is not None:
            return modified
        return await handler(request)

    # ------------------------------------------------------------------
    # 核心逻辑
    # ------------------------------------------------------------------

    def _handle_conflict(self, request: Any) -> ToolMessage | None:
        """处理 write_file 冲突。

        Returns:
            ToolMessage 表示已处理（跳过/重定向），None 表示放行。
        """
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        if str(tool_name) not in _WRITE_TOOLS:
            return None

        args = _mapping_value(tool_call, "args")
        if not isinstance(args, dict):
            return None

        file_path_str = args.get("file_path") or args.get("path") or ""
        if not isinstance(file_path_str, str) or not file_path_str:
            return None

        file_path = Path(file_path_str)

        # 检查文件是否已存在
        if not file_path.exists():
            return None  # 文件不存在，放行

        # 文件已存在，按策略处理
        if self.conflict_strategy == "overwrite":
            return None  # 直接放行（覆盖）

        if self.conflict_strategy == "skip":
            return self._skip_message(request, file_path)

        # timestamp_suffix 策略
        return self._redirect_with_suffix(request, file_path)

    def _redirect_with_suffix(self, request: Any, file_path: Path) -> ToolMessage:
        """重写目标路径，追加时间戳后缀。"""
        tool_call = getattr(request, "tool_call", {})
        tool_call_id = _mapping_value(tool_call, "id")

        # 构造新路径：在 stem 后追加时间戳
        timestamp = datetime.now().strftime(self.suffix_format)
        new_stem = file_path.stem + timestamp
        new_path = file_path.with_stem(new_stem)

        # 修改 request 中的路径参数
        args = _mapping_value(tool_call, "args")
        if isinstance(args, dict):
            if "file_path" in args:
                args["file_path"] = str(new_path)
            elif "path" in args:
                args["path"] = str(new_path)

        logger.info(
            "File conflict: %s -> %s (timestamp_suffix)",
            file_path, new_path,
        )

        return ToolMessage(
            content=(
                f"目标文件 {file_path} 已存在，"
                f"已自动重定向到 {new_path}（追加时间戳后缀）。"
            ),
            name="write_file",
            tool_call_id=str(tool_call_id or ""),
        )

    def _skip_message(self, request: Any, file_path: Path) -> ToolMessage:
        """返回跳过写入的消息。"""
        tool_call = getattr(request, "tool_call", {})
        tool_call_id = _mapping_value(tool_call, "id")
        return ToolMessage(
            content=(
                f"目标文件 {file_path} 已存在，已跳过写入（策略：skip）。"
                "如需更新文件内容，请使用 edit_file 工具。"
            ),
            name="write_file",
            tool_call_id=str(tool_call_id or ""),
        )


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


__all__ = ["FileWriteGuardMiddleware"]
