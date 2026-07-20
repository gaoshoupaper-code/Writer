"""FileStateTrackerMiddleware — edit_file 目标字符串预检中间件（A2 D4 修剪版）。

职责（A2 修剪后）：
  在 wrap_tool_call hook 上拦截 edit_file 工具调用，校验 old_string 是否
  真实存在于磁盘当前文件内容中。不存在则直接返回 error ToolMessage，引导
  模型改走 read_file + write_file。直接拦掉注定失败的 edit_file，避免
  storybuilding 连环失败（原设计目标：第 4 轮 6 次 edit_file 失败）。

A2 D4 关键修剪：
  删除原实现的死代码部分：
    - `_file_states: dict[str, str]` 字段（只写不读，从未被消费）
    - `_update_file_states` 方法（after_model 钩子里维护 _file_states）
    - `after_model` / `aafter_model` 钩子（只调用 _update_file_states）
    - `_compute_hash` / `_should_track` / `track_extensions` 参数
  保留：
    - `wrap_tool_call` 的 edit_file 前 old_string 存在性预检（实际生效的部分）
    - `fallback_on_mismatch` 参数（控制返回错误消息还是放行）

设计依据：.claude/md/20260720_150000_trace交付物丢失与基础设施归因.md §D4
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


class FileStateTrackerMiddleware(AgentMiddleware):
    """edit_file 目标字符串预检中间件（A2 D4 修剪版）。

    edit_file 调用前校验 old_string 是否真实存在于磁盘当前文件内容中。
    不存在则返回 error ToolMessage，引导模型 read_file + write_file。
    """

    def __init__(self, *, fallback_on_mismatch: bool = True) -> None:
        """
        Args:
            fallback_on_mismatch: True=返回引导消息让模型改走 read+write；
                                  False=返回简洁错误消息。默认 True。
        """
        self.fallback_on_mismatch = fallback_on_mismatch

    # ------------------------------------------------------------------
    # 工具调用拦截（同步 / 异步）
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """拦截同步工具调用：校验 edit_file 目标字符串。"""
        if not self._is_edit_file(request):
            return handler(request)

        result = self._validate_edit_target(request)
        if result is not None:
            return result

        return handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """拦截异步工具调用：校验 edit_file 目标字符串。"""
        if not self._is_edit_file(request):
            return await handler(request)

        result = self._validate_edit_target(request)
        if result is not None:
            return result

        return await handler(request)

    # ------------------------------------------------------------------
    # 核心逻辑
    # ------------------------------------------------------------------

    def _is_edit_file(self, request: Any) -> bool:
        """判断是否为 edit_file 工具调用。"""
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        return str(tool_name) == "edit_file"

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

    def _validate_edit_target(self, request: Any) -> ToolMessage | None:
        """校验 edit_file 的 old_string 是否存在于当前文件内容中。

        Returns:
            ToolMessage 表示拦截（引导模型 read+write），None 表示放行。
        """
        file_path = self._get_file_path(request)
        if file_path is None:
            return None

        # 文件不存在 → 放行让 edit_file 自己报错（避免重复错误消息）
        if not file_path.exists():
            return None

        tool_call = getattr(request, "tool_call", {})
        args = _mapping_value(tool_call, "args")
        if not isinstance(args, dict):
            return None

        old_string = args.get("old_string", "")
        if not isinstance(old_string, str) or not old_string:
            return None

        # 读取当前磁盘文件内容
        try:
            current_content = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Cannot read %s for edit validation: %s", file_path, exc)
            return None

        # 目标字符串存在 → 放行
        if old_string in current_content:
            return None

        # 目标字符串不存在 → 拦截
        tool_call_id = _mapping_value(tool_call, "id")
        logger.info(
            "edit_file target not found in %s, suggesting fallback to read+write",
            file_path,
        )
        if self.fallback_on_mismatch:
            content_msg = (
                f"edit_file 失败：在 {file_path} 中未找到目标字符串。"
                "文件内容可能已被修改，建议先使用 read_file 读取当前文件内容，"
                "确认最新内容后再使用 write_file 写入完整新内容。"
            )
        else:
            content_msg = (
                f"edit_file 失败：在 {file_path} 中未找到目标字符串。"
                "请先使用 read_file 读取当前文件内容，确认最新内容后再编辑。"
            )
        return ToolMessage(
            content=content_msg,
            name="edit_file",
            tool_call_id=str(tool_call_id or ""),
            status="error",
        )


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


__all__ = ["FileStateTrackerMiddleware"]
