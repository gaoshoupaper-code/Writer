"""Executor 平台拥有的最低交付物快照中间件。"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage


_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
_VERSIONED_ARTIFACT_PATHS = frozenset({
    "/demand.md",
    "/outline.md",
    "/storyline.md",
    "/worldview.md",
    "/novel.md",
})
_VERSIONED_ARTIFACT_PREFIXES = (
    "/character/",
    "/storyline/",
    "/detail/",
    "/chapter/",
    "/review/",
)


class EvidenceCaptureError(RuntimeError):
    """写入已成功，但严格测试所需的不可变取证失败。"""


class PlatformArtifactCaptureMiddleware(AgentMiddleware):
    """在工具成功写盘后，由当前 executor 强制冻结 ArtifactRevision。"""

    def __init__(
        self,
        *,
        recorder: Any,
        trace_id: str,
        workspace_root: Path,
        agent_name: str,
        strict: bool,
    ) -> None:
        self.recorder = recorder
        self.trace_id = trace_id
        self.workspace_root = workspace_root
        self.agent_name = agent_name
        self.strict = strict

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if not self._is_supported_write(request):
            return handler(request)
        result = handler(request)
        self._capture_if_successful(request, result)
        return result

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        if not self._is_supported_write(request):
            return await handler(request)
        result = await handler(request)
        self._capture_if_successful(request, result)
        return result

    def _is_supported_write(self, request: Any) -> bool:
        tool_call = getattr(request, "tool_call", {})
        if str(_mapping_value(tool_call, "name") or "") not in _WRITE_TOOLS:
            return False
        args = _mapping_value(tool_call, "args") or {}
        path = _normalize_path(_mapping_value(args, "file_path") or _mapping_value(args, "path"))
        return _is_versioned_artifact_path(path)

    def _capture_if_successful(self, request: Any, result: Any) -> None:
        if isinstance(result, ToolMessage) and getattr(result, "status", None) == "error":
            return
        tool_call = getattr(request, "tool_call", {})
        args = _mapping_value(tool_call, "args") or {}
        file_path = _normalize_path(
            _mapping_value(args, "file_path") or _mapping_value(args, "path")
        )
        tool_call_id = str(_mapping_value(tool_call, "id") or "")
        tool_name = str(_mapping_value(tool_call, "name") or "")
        try:
            content = self._read_workspace_file(file_path)
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            self.recorder.record_artifact_revision(
                self.trace_id,
                self.agent_name,
                file_path=file_path,
                content=content,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                content_hash=content_hash,
            )
        except Exception as exc:
            marker = getattr(self.recorder, "_mark_capture_degraded", None)
            if callable(marker):
                marker(
                    self.trace_id,
                    f"platform_artifact_capture_failed:{tool_call_id or 'missing'}:"
                    f"{file_path}:{type(exc).__name__}",
                )
            if self.strict:
                raise EvidenceCaptureError(
                    f"ArtifactRevision capture failed for {tool_name} "
                    f"{file_path} ({tool_call_id or 'missing tool_call_id'}): {exc}"
                ) from exc

    def _read_workspace_file(self, file_path: str) -> str:
        root = self.workspace_root.resolve()
        target = (root / file_path.lstrip("/")).resolve()
        target.relative_to(root)
        return target.read_text(encoding="utf-8")


def _mapping_value(mapping: object, key: str) -> Any:
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


def _normalize_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return "/" + value.replace("\\", "/").lstrip("/")


def _is_versioned_artifact_path(path: str) -> bool:
    return path in _VERSIONED_ARTIFACT_PATHS or path.startswith(_VERSIONED_ARTIFACT_PREFIXES)


__all__ = ["EvidenceCaptureError", "PlatformArtifactCaptureMiddleware"]
