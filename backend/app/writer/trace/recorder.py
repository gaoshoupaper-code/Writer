from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from app.schemas.screenplay import ThreadSummary
from app.writer.trace.projector import TraceProjector
from app.writer.trace.schemas import TraceDetail, TraceLogEvent, TraceRunSummary
from app.writer.trace.summary_export import export_trace_summary


@dataclass
class TraceRunHandle:
    trace_id: str
    queue: asyncio.Queue[TraceLogEvent] = field(default_factory=asyncio.Queue)


class TraceRecorder:
    def __init__(self) -> None:
        self._locks: dict[str, RLock] = {}
        self._sequences: dict[str, int] = {}
        self._queues: dict[str, asyncio.Queue[TraceLogEvent]] = {}
        self._started_monotonic: dict[str, float] = {}
        self._run_paths: dict[str, Path] = {}
        self._parents: dict[str, str | None] = {}
        self._run_kinds: dict[str, str] = {}
        self._run_names: dict[str, str] = {}
        self._run_metadata: dict[str, dict[str, Any]] = {}
        self._projector = TraceProjector()

    def create_run(self, thread: ThreadSummary, endpoint: str) -> TraceRunHandle:
        trace_id = f"trace-{uuid4().hex}"
        started_at = datetime.now(UTC)
        run_path = self._trace_path(thread, trace_id, started_at)
        run_path.parent.mkdir(parents=True, exist_ok=True)

        self._locks[trace_id] = RLock()
        self._sequences[trace_id] = 0
        self._queues[trace_id] = asyncio.Queue()
        self._started_monotonic[trace_id] = time.perf_counter()
        self._run_paths[trace_id] = run_path

        summary = TraceRunSummary(
            trace_id=trace_id,
            workspace_id=thread.workspace_id,
            thread_id=thread.thread_id,
            session_name=thread.session_name,
            workspace_path=thread.workspace_path,
            endpoint=endpoint,
            status="running",
            started_at=started_at.isoformat(),
            path=self._relative_trace_path(thread, trace_id, started_at),
        )
        self._write_run_index(thread, summary)
        handle = TraceRunHandle(trace_id=trace_id, queue=self._queues[trace_id])
        self.append_event(
            trace_id,
            {
                "type": "run_start",
                "status": "running",
                "source": "system",
                "input": {
                    "endpoint": endpoint,
                    "thread_id": thread.thread_id,
                    "workspace_id": thread.workspace_id,
                    "session_name": thread.session_name,
                },
            },
        )
        return handle

    def resume_run(self, thread: ThreadSummary, trace_id: str) -> tuple[TraceRunHandle, bool]:
        """续接活跃 trace（HITL resume 缝合点3）。

        内存活跃（_queues 命中）→ 复用 queue/lock/sequence/monotonic/run_path，
            is_new=False（不发 run_start，前端主动激活 trace-1）。
        不活跃（服务重启等内存丢失）→ 降级 create_run，is_new=True（发 run_start）。← D2=A
        """
        if trace_id in self._queues:
            return TraceRunHandle(trace_id=trace_id, queue=self._queues[trace_id]), False
        return self.create_run(thread, "screenplay.generate.stream"), True

    def register_run(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        kind: str,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        key = str(run_id)
        self._parents[key] = str(parent_run_id) if parent_run_id else None
        self._run_kinds[key] = kind
        if name:
            self._run_names[key] = name
        if metadata:
            self._run_metadata[key] = metadata

    def run_parent(self, run_id: UUID | str | None) -> str | None:
        if run_id is None:
            return None
        return self._parents.get(str(run_id))

    def run_kind(self, run_id: UUID | str | None) -> str | None:
        if run_id is None:
            return None
        return self._run_kinds.get(str(run_id))

    def run_name(self, run_id: UUID | str | None) -> str | None:
        if run_id is None:
            return None
        return self._run_names.get(str(run_id))

    def run_metadata(self, run_id: UUID | str | None) -> dict[str, Any] | None:
        if run_id is None:
            return None
        return self._run_metadata.get(str(run_id))

    def append_event(self, trace_id: str, values: dict[str, Any]) -> TraceLogEvent:
        lock = self._lock_for(trace_id)
        with lock:
            sequence = self._sequences.get(trace_id)
            if sequence is None:
                raise KeyError(f"Trace run is not active: {trace_id}")
            sequence += 1
            self._sequences[trace_id] = sequence

            event_type = str(values["type"])
            event = TraceLogEvent(
                trace_id=trace_id,
                event_id=str(values.get("event_id") or f"{trace_id}-{sequence}"),
                sequence=sequence,
                type=values["type"],
                status=values["status"],
                timestamp=str(values.get("timestamp") or self._now()),
                source=values["source"],
                duration_ms=values.get("duration_ms"),
                run_id=self._optional_str(values.get("run_id")),
                parent_run_id=self._optional_str(values.get("parent_run_id")),
                parent_event_id=self._optional_str(values.get("parent_event_id")),
                agent_name=self._optional_str(values.get("agent_name")),
                node_name=self._optional_str(values.get("node_name")),
                model_name=self._optional_str(values.get("model_name")),
                input=values.get("input"),
                output=_sanitize_tool_call_inputs(values.get("output")),
                usage=values.get("usage"),
                tool_calls=_tool_calls_payload(values.get("tool_calls")),
                tool_call_id=self._optional_str(values.get("tool_call_id")),
                tool_name=self._optional_str(values.get("tool_name")),
                tool_args=None,
                tool_output=_sanitize_tool_call_inputs(values.get("tool_output")),
                error=self._optional_str(values.get("error")),
            )

            run_path = self._run_paths.get(trace_id)
            if run_path is None:
                raise KeyError(f"Trace path is not registered: {trace_id}")
            with run_path.open("a", encoding="utf-8") as file:
                file.write(event.model_dump_json(exclude_none=True) + "\n")

            queue = self._queues.get(trace_id)
            if queue is not None:
                queue.put_nowait(event)
            return event

    def complete_run(self, thread: ThreadSummary, trace_id: str) -> TraceLogEvent:
        duration_ms = self._duration_ms(trace_id)
        event = self.append_event(
            trace_id,
            {
                "type": "run_end",
                "status": "completed",
                "source": "system",
                "duration_ms": duration_ms,
            },
        )
        self._finalize_run(thread, trace_id, "completed", duration_ms, None)
        return event

    def fail_run(self, thread: ThreadSummary, trace_id: str, error: BaseException) -> TraceLogEvent:
        return self._fail_run(thread, trace_id, f"{error.__class__.__name__}: {error}")

    def cancel_run(self, thread: ThreadSummary, trace_id: str) -> TraceLogEvent:
        # CancelledError 既可能是用户主动停止，也可能是 cloudflared/Cloudflare 因空闲
        # 超时掐断连接 —— 这里只如实描述触发条件，不臆断成"用户停止"。
        return self._fail_run(thread, trace_id, "Stream cancelled (client disconnected or user stopped)")

    def _fail_run(self, thread: ThreadSummary, trace_id: str, error_message: str) -> TraceLogEvent:
        duration_ms = self._duration_ms(trace_id)
        event = self.append_event(
            trace_id,
            {
                "type": "run_error",
                "status": "failed",
                "source": "system",
                "duration_ms": duration_ms,
                "error": error_message,
            },
        )
        self._finalize_run(thread, trace_id, "failed", duration_ms, error_message)
        return event

    def list_runs(self, thread: ThreadSummary) -> list[TraceRunSummary]:
        runs = self._read_run_index(thread)
        summaries = [TraceRunSummary.model_validate(run) for run in runs.values() if run["thread_id"] == thread.thread_id]
        return sorted(summaries, key=lambda run: run.started_at, reverse=True)

    def read_run(self, thread: ThreadSummary, trace_id: str) -> TraceDetail | None:
        return self._read_run_detail(thread, trace_id)

    def delete_run(self, thread: ThreadSummary, trace_id: str) -> bool:
        runs = self._read_run_index(thread)
        run = runs.get(trace_id)
        if run is None or run.get("thread_id") != thread.thread_id:
            return False
        self._delete_run_from_index(thread, runs, trace_id, run)
        self._write_index(thread, runs)
        return True

    def delete_thread_runs(self, thread: ThreadSummary) -> int:
        runs = self._read_run_index(thread)
        thread_runs = [
            (trace_id, run)
            for trace_id, run in runs.items()
            if run.get("thread_id") == thread.thread_id
        ]
        for trace_id, run in thread_runs:
            self._delete_run_from_index(thread, runs, trace_id, run)
        self._write_index(thread, runs)
        return len(thread_runs)

    def read_run_snapshot(self, thread: ThreadSummary, trace_id: str) -> TraceDetail | None:
        return self._read_run_detail(thread, trace_id)

    def get_active_queue(self, trace_id: str) -> asyncio.Queue[TraceLogEvent] | None:
        return self._queues.get(trace_id)

    def _read_run_detail(self, thread: ThreadSummary, trace_id: str) -> TraceDetail | None:
        run_data = self._read_run_index(thread).get(trace_id)
        if run_data is None:
            return None
        run = TraceRunSummary.model_validate(run_data)
        trace_path = Path(thread.workspace_path) / run.path
        if not trace_path.exists():
            raise FileNotFoundError(f"Trace file is missing: {trace_path}")
        events = self._read_events(trace_path)
        if run.status == "running":
            run.event_count = len(events)
        projection = self._projector.project(run, events)
        return TraceDetail(
            run=run,
            events=events,
            nodes=projection.nodes,
            context=projection.context,
            todos=projection.todos,
        )

    def _read_events(self, trace_path: Path) -> list[TraceLogEvent]:
        events_by_id: dict[str, TraceLogEvent] = {}
        with trace_path.open("r", encoding="utf-8") as file:
            for line in file:
                stripped = line.strip()
                if not stripped:
                    continue
                event_data = json.loads(stripped)
                if event_data.get("type") == "run_link" and event_data.get("source") == "callback":
                    continue
                event = TraceLogEvent.model_validate(_sanitize_event_data(event_data))
                events_by_id.setdefault(event.event_id, event)
        return sorted(events_by_id.values(), key=lambda event: (event.timestamp, event.sequence))

    def _finalize_run(
        self,
        thread: ThreadSummary,
        trace_id: str,
        status: str,
        duration_ms: int,
        error: str | None,
    ) -> None:
        runs = self._read_run_index(thread)
        run = runs.get(trace_id)
        if run is None:
            raise KeyError(f"Trace run not found in index: {trace_id}")
        run["status"] = status
        run["ended_at"] = self._now()
        run["duration_ms"] = duration_ms
        run["event_count"] = self._sequences[trace_id]
        run["error"] = error
        runs[trace_id] = run
        self._write_index(thread, runs)

        # 导出模型可读的执行记录摘要（包括失败的 trace）
        self._export_summary(thread, trace_id, run)

        self._cleanup_run_state(trace_id)

    def _export_summary(
        self,
        thread: ThreadSummary,
        trace_id: str,
        run_data: dict[str, Any],
    ) -> None:
        """trace 结束后自动导出摘要 JSON。"""
        try:
            trace_path = Path(thread.workspace_path) / str(run_data["path"])
            if not trace_path.exists():
                return
            events = self._read_events(trace_path)
            run_summary = TraceRunSummary.model_validate(run_data)
            projection = self._projector.project(run_summary, events)
            detail = TraceDetail(
                run=run_summary,
                events=events,
                nodes=projection.nodes,
                context=projection.context,
                todos=projection.todos,
            )
            summary_path = trace_path.with_name(f"{trace_id}_summary.json")
            export_trace_summary(detail, summary_path)
        except Exception:
            # 摘要导出失败不应影响 trace 正常结束流程
            pass

    def _delete_run_from_index(
        self,
        thread: ThreadSummary,
        runs: dict[str, dict[str, Any]],
        trace_id: str,
        run: dict[str, Any],
    ) -> None:
        if run.get("status") == "running" or trace_id in self._queues or trace_id in self._run_paths:
            raise ValueError("Trace is still running")

        trace_path = Path(thread.workspace_path) / str(run["path"])
        if trace_path.exists():
            trace_path.unlink()
        # 同时清理摘要文件
        summary_path = trace_path.with_name(f"{trace_id}_summary.json")
        if summary_path.exists():
            summary_path.unlink()
        del runs[trace_id]
        self._cleanup_run_state(trace_id)

    def _cleanup_run_state(self, trace_id: str) -> None:
        self._queues.pop(trace_id, None)
        self._started_monotonic.pop(trace_id, None)
        self._run_paths.pop(trace_id, None)
        self._locks.pop(trace_id, None)
        self._sequences.pop(trace_id, None)

    def _read_run_index(self, thread: ThreadSummary) -> dict[str, dict[str, Any]]:
        index_path = self._index_path(thread)
        if not index_path.exists():
            return {}
        with index_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError(f"Invalid trace index format: {index_path}")
        return data

    def _write_run_index(self, thread: ThreadSummary, run: TraceRunSummary) -> None:
        runs = self._read_run_index(thread)
        runs[run.trace_id] = run.model_dump()
        self._write_index(thread, runs)

    def _write_index(self, thread: ThreadSummary, runs: dict[str, dict[str, Any]]) -> None:
        index_path = self._index_path(thread)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with index_path.open("w", encoding="utf-8") as file:
            json.dump(runs, file, ensure_ascii=False, indent=2)

    def _index_path(self, thread: ThreadSummary) -> Path:
        return Path(thread.workspace_path) / "traces" / "index.json"

    def _trace_path(self, thread: ThreadSummary, trace_id: str, started_at: datetime) -> Path:
        return Path(thread.workspace_path) / self._relative_trace_path(thread, trace_id, started_at)

    def _relative_trace_path(self, thread: ThreadSummary, trace_id: str, started_at: datetime) -> str:
        return f"traces/{started_at.strftime('%Y%m%d-%H%M')}/{trace_id}.jsonl"

    def _lock_for(self, trace_id: str) -> RLock:
        lock = self._locks.get(trace_id)
        if lock is None:
            raise KeyError(f"Trace lock is not registered: {trace_id}")
        return lock

    def _duration_ms(self, trace_id: str) -> int:
        started = self._started_monotonic.get(trace_id)
        if started is None:
            raise KeyError(f"Trace start time is not registered: {trace_id}")
        return int((time.perf_counter() - started) * 1000)

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _optional_str(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)


def _sanitize_event_data(event_data: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event_data.get("type") or "")
    sanitized = dict(event_data)
    if event_type.startswith("llm"):
        pass  # input 保留：包含模型完整输入（系统提示词、注入上下文、对话历史）
    sanitized.pop("tool_args", None)
    if "output" in sanitized:
        sanitized["output"] = _sanitize_tool_call_inputs(sanitized["output"])
    if "tool_output" in sanitized:
        sanitized["tool_output"] = _sanitize_tool_call_inputs(sanitized["tool_output"])
    if "tool_calls" in sanitized:
        sanitized["tool_calls"] = _tool_calls_payload(sanitized["tool_calls"])
    return sanitized


def _sanitize_tool_call_inputs(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"tool_calls", "invalid_tool_calls"}:
                sanitized[str(key)] = _tool_calls_payload(item) or []
            else:
                sanitized[str(key)] = _sanitize_tool_call_inputs(item)
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_sanitize_tool_call_inputs(item) for item in value]
    return value


def _tool_calls_payload(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return None
    return [_tool_call_summary(call) for call in value]


def _tool_call_summary(call: Any) -> dict[str, Any]:
    if not isinstance(call, Mapping):
        return {"name": str(call)}
    summary: dict[str, Any] = {}
    for key in ("name", "id", "type"):
        value = call.get(key)
        if value not in (None, ""):
            summary[str(key)] = value
    return summary or {"name": "unknown"}
