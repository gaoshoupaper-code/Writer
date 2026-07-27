"""ArtifactSnapshotMiddleware — 产物修订不可变快照中间件（第二期，2026-07）。

职责：
  在 wrap_tool_call hook 上拦截 write_file / edit_file，写盘成功后（result 非 error）
  调 artifact_snapshot_callback，把文件路径 + 写入后完整内容 + sha256 指纹传出去。

callback 把快照写进 trace 的 run_meta 事件（input.artifact_snapshot 键），
供证据编译器重建产物修订时间线，实现 trace-time provenance。

装配位置：WriteResultInspectorMiddleware 之后（最内层），确保：
  - 在 FileWriteSerialize 串行化锁之内（不会和并发写同一文件交错）
  - 在 EncodingGuard 编码校验之后（快照内容一定是合法 UTF-8 + 完整的）
  - 在 WriteResultInspector 之后（只有写盘成功的才快照，error 的已被转抛）

设计依据：.claude/md/20260726_214821_trace证据管线.md R9（产物保真）
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

_WRITE_TOOLS = frozenset({"write_file", "edit_file"})


class ArtifactSnapshotMiddleware(AgentMiddleware):
    """产物修订快照中间件。

    拦 write_file / edit_file，写盘成功后调 callback 冻结快照。
    快照失败静默吞掉（不影响写作流程），与 quality_callback 一致。
    """

    def __init__(
        self,
        snapshot_callback: Callable[[dict[str, Any]], None],
        workspace_root: Path,
        agent_name: str = "",
    ) -> None:
        self._callback = snapshot_callback
        self._workspace_root = workspace_root
        self._agent_name = agent_name

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """同步：写盘成功后冻结快照。"""
        if not self._is_write_tool(request):
            return handler(request)
        result = handler(request)
        self._snapshot_if_ok(request, result)
        return result

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """异步：写盘成功后冻结快照。"""
        if not self._is_write_tool(request):
            return await handler(request)
        result = await handler(request)
        self._snapshot_if_ok(request, result)
        return result

    # ------------------------------------------------------------------
    # 内部逻辑
    # ------------------------------------------------------------------

    def _is_write_tool(self, request: Any) -> bool:
        """判断是否为写入工具（write_file / edit_file）。"""
        tool_call = getattr(request, "tool_call", {})
        tool_name = _mapping_value(tool_call, "name")
        return str(tool_name) in _WRITE_TOOLS

    def _snapshot_if_ok(self, request: Any, result: Any) -> None:
        """写盘成功后冻结快照。error 的 ToolMessage 跳过（WriteResultInspector 会转抛）。"""
        # error 的 result 不快照
        if isinstance(result, ToolMessage) and getattr(result, "status", None) == "error":
            return

        try:
            tool_call = getattr(request, "tool_call", {})
            args = _mapping_value(tool_call, "args") or {}
            tool_name = str(_mapping_value(tool_call, "name") or "")

            file_path = args.get("file_path") or args.get("path") or ""
            if not file_path:
                return

            # 拿写入后的完整内容
            content = self._get_written_content(tool_name, args, file_path)
            if content is None:
                return

            fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()

            self._callback({
                "agent_name": self._agent_name,
                "file_path": file_path,
                "tool": tool_name,
                "content": content,
                "fingerprint": fingerprint,
            })
        except Exception:
            # 快照失败不影响写作流程（静默吞掉，与 quality_callback 一致）
            logger.debug("产物快照失败", exc_info=True)

    def _get_written_content(self, tool_name: str, args: dict, file_path: str) -> str | None:
        """获取写入后的完整内容。

        write_file：args.content 就是完整内容（deepagents write_file 签名含 content）。
        edit_file：args 只有 old_string/new_string，需从磁盘读最终内容。
        """
        if tool_name == "write_file":
            content = args.get("content")
            return content if isinstance(content, str) else None

        # edit_file：读磁盘最终态
        rel = file_path.lstrip("/")
        abs_path = self._workspace_root / rel
        try:
            return abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None


def _mapping_value(mapping: object, key: str) -> Any:
    """安全地从字典或对象中取值。"""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


__all__ = ["ArtifactSnapshotMiddleware"]
