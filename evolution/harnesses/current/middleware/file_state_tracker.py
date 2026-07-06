"""FileStateTrackerMiddleware — 文件状态追踪与 edit_file fallback 中间件。

职责：
  在 after_model hook 上追踪文件写入/编辑后的内容摘要（hash），
  在后续 edit_file 调用前校验目标字符串是否存在于当前文件内容中。
  若不存在，自动 fallback 到 read_file+write_file 模式。
  解决 storybuilding 第4轮连续 6 次 edit_file 因字符串不匹配失败的问题。

使用方式：
  装配到 agent 的 after_model hook 处理器列表。
  track_extensions: 追踪的文件扩展名列表（默认 [".md"]）
  fallback_on_mismatch: 字符串不匹配时是否自动 fallback（默认 True）
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

# 默认追踪的文件扩展名
_DEFAULT_TRACK_EXTENSIONS = [".md"]


class FileStateTrackerMiddleware(AgentMiddleware):
    """文件状态追踪与 edit_file fallback 中间件。

    追踪文件写入/编辑后的内容摘要，在 edit_file 调用前校验目标字符串
    是否存在于当前文件内容中。若不存在，自动 fallback 到 read_file+write_file 模式。
    """

    def __init__(
        self,
        *,
        track_extensions: list[str] | None = None,
        fallback_on_mismatch: bool = True,
    ) -> None:
        """
        Args:
            track_extensions: 追踪的文件扩展名列表，默认 [".md"]
            fallback_on_mismatch: 字符串不匹配时是否自动 fallback，默认 True
        """
        self.track_extensions = set(track_extensions or _DEFAULT_TRACK_EXTENSIONS)
        self.fallback_on_mismatch = fallback_on_mismatch
        # file_path -> content_hash（文件内容的 SHA256 摘要）
        self._file_states: dict[str, str] = {}

    # ------------------------------------------------------------------
    # after_model hook：模型输出后追踪文件状态
    # ------------------------------------------------------------------

    def after_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步：模型输出后更新文件状态。"""
        self._update_file_states(state)
        return None

    async def aafter_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """异步：模型输出后更新文件状态。"""
        self._update_file_states(state)
        return None

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

    def _should_track(self, file_path: Path) -> bool:
        """判断文件扩展名是否在追踪范围内。"""
        return file_path.suffix in self.track_extensions

    def _compute_hash(self, content: str) -> str:
        """计算内容 SHA256 摘要。"""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _update_file_states(self, state: Any) -> None:
        """从 state 中提取文件写入/编辑操作，更新文件状态。"""
        messages = getattr(state, "get", lambda key: [])(key="messages") if hasattr(state, "get") else []
        if not messages:
            return

        for msg in messages:
            if hasattr(msg, "additional_kwargs"):
                kwargs = msg.additional_kwargs
                # 检查是否有文件写入操作
                tool_calls = kwargs.get("tool_calls", []) if isinstance(kwargs, dict) else []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    name = tc.get("name", "")
                    if name not in ("write_file", "edit_file"):
                        continue
                    args = tc.get("args", {})
                    if not isinstance(args, dict):
                        continue
                    path_str = args.get("file_path") or args.get("path") or ""
                    content = args.get("content") or args.get("text") or ""
                    if isinstance(path_str, str) and path_str and isinstance(content, str):
                        self._file_states[path_str] = self._compute_hash(content)

    def _validate_edit_target(self, request: Any) -> ToolMessage | None:
        """校验 edit_file 的目标字符串是否存在于当前文件内容中。

        Returns:
            ToolMessage 表示 fallback 处理，None 表示放行。
        """
        file_path = self._get_file_path(request)
        if file_path is None:
            return None

        if not self._should_track(file_path):
            return None

        # 检查文件是否存在
        if not file_path.exists():
            return None

        # 获取 edit_file 的 old_string 参数
        tool_call = getattr(request, "tool_call", {})
        args = _mapping_value(tool_call, "args")
        if not isinstance(args, dict):
            return None

        old_string = args.get("old_string", "")
        if not isinstance(old_string, str) or not old_string:
            return None

        # 读取当前文件内容
        try:
            current_content = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Cannot read %s for edit validation: %s", file_path, exc)
            return None

        # 校验目标字符串是否存在
        if old_string in current_content:
            return None  # 目标字符串存在，放行

        # 目标字符串不存在
        if not self.fallback_on_mismatch:
            # 不 fallback，返回错误消息
            tool_call_id = _mapping_value(tool_call, "id")
            return ToolMessage(
                content=(
                    f"edit_file 失败：在 {file_path} 中未找到目标字符串。"
                    "请先使用 read_file 读取当前文件内容，确认最新内容后再编辑。"
                ),
                name="edit_file",
                tool_call_id=str(tool_call_id or ""),
            )

        # fallback 模式：将 edit_file 转为 read_file + write_file
        # 返回提示消息，引导模型重新读取文件后重试
        tool_call_id = _mapping_value(tool_call, "id")
        logger.info(
            "edit_file target not found in %s, suggesting fallback to read_file+write_file",
            file_path,
        )
        return ToolMessage(
            content=(
                f"edit_file 失败：在 {file_path} 中未找到目标字符串。"
                "文件内容可能已被修改，建议先使用 read_file 读取当前文件内容，"
                "确认最新内容后再使用 write_file 写入完整新内容。"
            ),
            name="edit_file",
            tool_call_id=str(tool_call_id or ""),
        )


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


__all__ = ["FileStateTrackerMiddleware"]
