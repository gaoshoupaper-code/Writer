"""Executor 平台拥有的最低交付物快照中间件。"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


# 临时诊断：monkey-patch FilesystemBackend.write，记录每次调用的线程/路径/结果，
# 定位「ToolMessage success 但文件不存在」。导入即生效，定位后移除。
try:
    import os as _os
    import threading as _threading
    import deepagents.backends.filesystem as _fs

    _orig_write = _fs.FilesystemBackend.write

    def _patched_write(self, file_path, content):  # type: ignore[no-untyped-def]
        tid = _threading.get_ident()
        try:
            resolved = self._resolve_path(file_path)
            existed_before = resolved.exists()
        except Exception as _e:
            resolved = f"<unresolvable:{_e!r}>"
            existed_before = "unresolvable"
        result = _orig_write(self, file_path, content)
        try:
            exists_after = _os.path.exists(str(resolved)) if isinstance(resolved, _os.PathLike) or isinstance(resolved, str) else "?"
        except Exception:
            exists_after = "?"
        wr_err = getattr(result, "error", None)
        logger.warning(
            "FSWRITE DIAG tid=%s file=%r resolved=%s existed_before=%s "
            "exists_after=%s result=%s error=%r",
            tid, file_path, resolved, existed_before, exists_after,
            type(result).__name__, wr_err,
        )
        return result

    if not getattr(_fs.FilesystemBackend.write, "_patched", False):
        _patched_write._patched = True  # type: ignore[attr-defined]
        _fs.FilesystemBackend.write = _patched_write
        logger.warning("FSWRITE DIAG patch installed")
except Exception as _e:
    logger.warning("FSWRITE DIAG patch FAILED: %r", _e)


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
        # 临时诊断（同步路径）：并行 write_file 实际走同步 wrap_tool_call，
        # 记录 handler 前后文件状态，区分 handler 没跑 vs write 内部失败。
        tool_call = getattr(request, "tool_call", {})
        args = _mapping_value(tool_call, "args") or {}
        _dpath = _normalize_path(
            _mapping_value(args, "file_path") or _mapping_value(args, "path")
        )
        _dcid = str(_mapping_value(tool_call, "id") or "")[-10:]
        _dbefore = self._exists_for_diag(_dpath)
        try:
            result = handler(request)
        except BaseException as _hexc:
            logger.warning(
                "WRAP DIAG handler-raised trace_id=%s file=%s call=%s "
                "before=%s exc=%r",
                self.trace_id, _dpath, _dcid, _dbefore, _hexc,
            )
            raise
        _dafter = self._exists_for_diag(_dpath)
        logger.warning(
            "WRAP DIAG trace_id=%s file=%s call=%s result=%s "
            "before=%s after=%s",
            self.trace_id, _dpath, _dcid,
            type(result).__name__, _dbefore, _dafter,
        )
        self._capture_if_successful(request, result)
        return result

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        if not self._is_supported_write(request):
            return await handler(request)
        # 临时诊断：记录 handler（真正 write）执行前后文件状态，定位
        # 「ToolMessage success 但文件不存在」是 handler 没跑还是 write 内部失败。
        tool_call = getattr(request, "tool_call", {})
        args = _mapping_value(tool_call, "args") or {}
        _dpath = _normalize_path(
            _mapping_value(args, "file_path") or _mapping_value(args, "path")
        )
        _dcid = str(_mapping_value(tool_call, "id") or "")[-10:]
        _dbefore = self._exists_for_diag(_dpath)
        try:
            result = await handler(request)
        except BaseException as _hexc:
            logger.warning(
                "AWRAP DIAG handler-raised trace_id=%s file=%s call=%s "
                "before=%s exc=%r",
                self.trace_id, _dpath, _dcid, _dbefore, _hexc,
            )
            raise
        _dafter = self._exists_for_diag(_dpath)
        logger.warning(
            "AWRAP DIAG trace_id=%s file=%s call=%s result=%s "
            "before=%s after=%s",
            self.trace_id, _dpath, _dcid,
            type(result).__name__, _dbefore, _dafter,
        )
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
        # 临时诊断：打印调用栈，定位谁调用了 _capture_if_successful
        # （WRAP/AWRAP 诊断 0 条但此方法被调用 → 说明走了别的调用路径）
        import traceback
        stk = traceback.format_stack()
        # 精简：只保留非 stdlib 的关键帧
        slim = [s.strip() for s in stk if "site-packages" not in s and "traceback" not in s]
        logger.warning(
            "CAPTURE-CALLER DIAG trace_id=%s file=%s stack=%s",
            self.trace_id,
            _normalize_path(
                (_mapping_value(getattr(request, "tool_call", {}), "args") or {}).get("file_path")
                or (_mapping_value(getattr(request, "tool_call", {}), "args") or {}).get("path")
                or ""
            ),
            " | ".join(slim[-6:]),
        )
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
            # 临时诊断（定位 EvidenceCaptureError 根因）：
            # write 已返回成功(result)，但回读文件失败。收集现场快照确认
            # 文件到底在不在、write 返回了什么、目录里有什么。
            diag = self._diagnostic_snapshot(file_path, result)
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
                    f"{file_path} ({tool_call_id or 'missing tool_call_id'}): {exc} "
                    f"| DIAG {diag}"
                ) from exc

    def _read_workspace_file(self, file_path: str) -> str:
        root = self.workspace_root.resolve()
        target = (root / file_path.lstrip("/")).resolve()
        target.relative_to(root)
        return target.read_text(encoding="utf-8")

    def _exists_for_diag(self, file_path: str) -> str:
        """临时诊断：返回文件存在性 + mtime（容错，仅供 AWRAP DIAG）。"""
        try:
            root = self.workspace_root.resolve()
            target = root / file_path.lstrip("/")
            if target.exists():
                return f"Y(m={target.stat().st_mtime:.4f})"
            return "N"
        except Exception as e:
            return f"err({type(e).__name__})"

    def _diagnostic_snapshot(self, file_path: str, write_result: Any) -> str:
        """临时诊断：write 已返回但回读失败时，收集现场快照。

        只在异常路径调用，不影响正常性能。确认文件物理存在性与 write 返回值，
        用于钉死「write 成功返回但文件不存在」的根因（路径错位 / 并发取消 / 其他）。
        全程容错——诊断自身绝不能掩盖原始异常。
        """
        try:
            root = self.workspace_root.resolve()
        except Exception as e:
            return f"root_resolve_err={e!r}"
        target = (root / file_path.lstrip("/"))
        try:
            target_resolved = target.resolve()
        except Exception as e:
            target_resolved = f"<unresolvable:{e!r}>"

        parts: list[str] = []
        # 1. write 返回值（成功标志）
        wr_type = type(write_result).__name__
        wr_error = getattr(write_result, "error", None)
        wr_path = getattr(write_result, "path", None)
        if wr_error:
            parts.append(f"write_result={wr_type}(ERROR={wr_error!r})")
        elif wr_path is not None:
            parts.append(f"write_result={wr_type}(OK path={wr_path!r})")
        else:
            # ToolMessage 或其他——只看 status，不打内容
            status = getattr(write_result, "status", None)
            parts.append(f"write_result={wr_type}(status={status!r})")

        # 2. 回读目标路径（绝对）
        parts.append(f"target={target_resolved}")
        parts.append(f"root_exists={root.exists()}")

        # 3. 目标文件存在性 + stat（含精确 mtime，可对照 LLM 写入时刻）
        try:
            st = target.stat()
            parts.append(
                f"target_exists=True size={st.st_size} "
                f"mtime={st.st_mtime:.6f}"
            )
        except FileNotFoundError:
            parts.append("target_exists=False(FileNotFoundError)")
        except Exception as e:
            parts.append(f"target_stat_err={type(e).__name__}:{e}")

        # 4. 父目录列表（看同批次其他文件在不在）
        parent = target.parent
        try:
            siblings = sorted(parent.iterdir(), key=lambda p: p.name)
            entries = []
            for child in siblings:
                try:
                    cst = child.stat()
                    entries.append(f"{child.name}({cst.st_size}B,m={cst.st_mtime:.4f})")
                except Exception:
                    entries.append(f"{child.name}(stat_err)")
            parts.append(f"parent={parent.name} entries={entries}")
        except Exception as e:
            parts.append(f"parent_list_err={type(e).__name__}:{e}")

        diag = " ".join(parts)
        logger.warning(
            "EvidenceCapture DIAG trace_id=%s agent=%s file=%s :: %s",
            self.trace_id, self.agent_name, file_path, diag,
        )
        return diag


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
