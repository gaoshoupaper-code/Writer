from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")

from app.writer.trace.schemas import (
    TraceContextRange,
    TraceContextSegment,
    TraceLogEvent,
    TraceNode,
    TraceRunSummary,
    TraceUsage,
    TraceTodoItem,
    TraceTodoSnapshot,
)

TODO_TOOL_NAMES = {"write_todos", "write_todo"}


@dataclass
class TraceProjection:
    nodes: list[TraceNode] = field(default_factory=list)
    context: list[TraceContextSegment] = field(default_factory=list)
    todos: list[TraceTodoSnapshot] = field(default_factory=list)


@dataclass
class _PendingEvent:
    event: TraceLogEvent
    node_id: str
    input_range: TraceContextRange | None = None


class TraceProjector:
    def project(self, run: TraceRunSummary, events: list[TraceLogEvent]) -> TraceProjection:
        projection = TraceProjection()
        if not events:
            projection.nodes.append(_run_node(run, []))
            return projection

        projection.nodes.append(_run_node(run, events))
        state = _ProjectionState(projection)
        llm_starts: dict[str, list[_PendingEvent]] = {}
        tool_starts: dict[str, list[_PendingEvent]] = {}

        for event in events:
            if event.type in {"llm_start", "llm_end", "llm_error", "tool_start", "tool_end", "tool_error"}:
                state.ensure_agent_node(event)
            if event.type == "llm_start":
                node_id = _llm_node_id(event)
                llm_starts.setdefault(_event_pair_key(event), []).append(_PendingEvent(event=event, node_id=node_id))
            elif event.type == "llm_end":
                start = _pop_pending(llm_starts, _event_pair_key(event))
                state.add_llm_node(start, event)
            elif event.type == "llm_error":
                start = _pop_pending(llm_starts, _event_pair_key(event))
                state.add_llm_error_node(start, event)
            elif event.type == "tool_start":
                node_id = _tool_node_id(event)
                tool_starts.setdefault(_event_pair_key(event), []).append(_PendingEvent(event=event, node_id=node_id))
            elif event.type == "tool_end":
                start = _pop_pending(tool_starts, _event_pair_key(event))
                state.add_tool_node(start, event)
            elif event.type == "tool_error":
                start = _pop_pending(tool_starts, _event_pair_key(event))
                state.add_tool_error_node(start, event)
            elif event.type == "run_error":
                state.add_run_error(event)

        for pending_events in llm_starts.values():
            for pending in pending_events:
                state.add_running_llm_node(pending)
        for pending_events in tool_starts.values():
            for pending in pending_events:
                state.add_running_tool_node(pending)

        return projection


class _ProjectionState:
    def __init__(self, projection: TraceProjection) -> None:
        self.projection = projection
        self.agent_nodes: set[str] = set()
        self.context_sequence = 0

    def ensure_agent_node(self, event: TraceLogEvent) -> None:
        if not event.agent_name:
            return
        node_id = _agent_node_id(event)
        if node_id in self.agent_nodes:
            return
        self.agent_nodes.add(node_id)
        self.projection.nodes.append(
            TraceNode(
                node_id=node_id,
                parent_node_id="run",
                kind="agent",
                label=event.agent_name,
                status="completed",
                agent_name=event.agent_name,
                agent_role=_agent_role(event.agent_name),
                depth=_agent_depth(event.agent_name),
                started_at=event.timestamp,
                raw_event_ids=[event.event_id],
            )
        )

    def add_llm_node(self, start: _PendingEvent | None, end: TraceLogEvent) -> None:
        node_id = start.node_id if start else _llm_node_id(end)
        anchor_id = self._append_llm_output(end, node_id)
        input_range = start.input_range if start else None
        raw_event_ids = _raw_ids(start.event if start else None, end)
        usage = end.usage or _usage_from_llm_output(end.output)
        self.projection.nodes.append(
            TraceNode(
                node_id=node_id,
                parent_node_id=_agent_node_id(end),
                kind="llm",
                label=_llm_label(end),
                status=end.status,
                agent_name=end.agent_name,
                agent_role=_agent_role(end.agent_name),
                depth=_agent_depth(end.agent_name),
                started_at=start.event.timestamp if start else None,
                ended_at=end.timestamp,
                duration_ms=end.duration_ms,
                model_name=end.model_name,
                usage=usage,
                context_anchor_id=_range_start(input_range) or anchor_id,
                input_context_range=input_range,
                output_context_anchor_id=anchor_id,
                raw_event_ids=raw_event_ids,
            )
        )

    def add_llm_error_node(self, start: _PendingEvent | None, error: TraceLogEvent) -> None:
        node_id = start.node_id if start else _llm_node_id(error)
        anchor_id = self._append_error(error, node_id, "LLM 失败")
        input_range = start.input_range if start else None
        self.projection.nodes.append(
            TraceNode(
                node_id=node_id,
                parent_node_id=_agent_node_id(error),
                kind="error",
                label=_llm_label(error),
                status="failed",
                agent_name=error.agent_name,
                agent_role=_agent_role(error.agent_name),
                depth=_agent_depth(error.agent_name),
                started_at=start.event.timestamp if start else None,
                ended_at=error.timestamp,
                duration_ms=error.duration_ms,
                model_name=error.model_name,
                context_anchor_id=_range_start(input_range) or anchor_id,
                input_context_range=input_range,
                output_context_anchor_id=anchor_id,
                raw_event_ids=_raw_ids(start.event if start else None, error),
                error=error.error,
            )
        )

    def add_running_llm_node(self, pending: _PendingEvent) -> None:
        event = pending.event
        self.projection.nodes.append(
            TraceNode(
                node_id=pending.node_id,
                parent_node_id=_agent_node_id(event),
                kind="llm",
                label=_llm_label(event),
                status="running",
                agent_name=event.agent_name,
                agent_role=_agent_role(event.agent_name),
                depth=_agent_depth(event.agent_name),
                started_at=event.timestamp,
                model_name=event.model_name,
                context_anchor_id=_range_start(pending.input_range),
                input_context_range=pending.input_range,
                raw_event_ids=[event.event_id],
            )
        )

    def add_tool_node(self, start: _PendingEvent | None, end: TraceLogEvent) -> None:
        tool_node_id = start.node_id if start else _tool_node_id(end)
        anchor_id = self._append_tool_output(end, tool_node_id)
        input_range = start.input_range if start else None
        tool_error = _tool_error(end.tool_output)
        self.projection.nodes.append(
            TraceNode(
                node_id=tool_node_id,
                parent_node_id=_agent_node_id(end),
                kind="tool",
                label=_tool_label(end),
                status="failed" if tool_error else end.status,
                agent_name=end.agent_name,
                agent_role=_agent_role(end.agent_name),
                depth=_agent_depth(end.agent_name),
                started_at=start.event.timestamp if start else None,
                ended_at=end.timestamp,
                duration_ms=end.duration_ms,
                tool_name=end.tool_name,
                context_anchor_id=_range_start(input_range) or anchor_id,
                input_context_range=input_range,
                output_context_anchor_id=anchor_id,
                raw_event_ids=_raw_ids(start.event if start else None, end),
                error=tool_error,
            )
        )
        if end.tool_name in TODO_TOOL_NAMES:
            self._append_todo(end, start.event if start else None, anchor_id)

    def add_tool_error_node(self, start: _PendingEvent | None, error: TraceLogEvent) -> None:
        node_id = start.node_id if start else _tool_node_id(error)
        anchor_id = self._append_error(error, node_id, "Tool 失败")
        input_range = start.input_range if start else None
        self.projection.nodes.append(
            TraceNode(
                node_id=node_id,
                parent_node_id=_agent_node_id(error),
                kind="error",
                label=_tool_label(error),
                status="failed",
                agent_name=error.agent_name,
                agent_role=_agent_role(error.agent_name),
                depth=_agent_depth(error.agent_name),
                started_at=start.event.timestamp if start else None,
                ended_at=error.timestamp,
                duration_ms=error.duration_ms,
                tool_name=error.tool_name,
                context_anchor_id=_range_start(input_range) or anchor_id,
                input_context_range=input_range,
                output_context_anchor_id=anchor_id,
                raw_event_ids=_raw_ids(start.event if start else None, error),
                error=error.error,
            )
        )

    def add_running_tool_node(self, pending: _PendingEvent) -> None:
        event = pending.event
        self.projection.nodes.append(
            TraceNode(
                node_id=pending.node_id,
                parent_node_id=_agent_node_id(event),
                kind="tool",
                label=_tool_label(event),
                status="running",
                agent_name=event.agent_name,
                agent_role=_agent_role(event.agent_name),
                depth=_agent_depth(event.agent_name),
                started_at=event.timestamp,
                tool_name=event.tool_name,
                context_anchor_id=_range_start(pending.input_range),
                input_context_range=pending.input_range,
                raw_event_ids=[event.event_id],
            )
        )

    def add_run_error(self, event: TraceLogEvent) -> None:
        node_id = f"error:{event.event_id}"
        anchor_id = self._append_error(event, node_id, "Run 失败")
        self.projection.nodes.append(
            TraceNode(
                node_id=node_id,
                parent_node_id="run",
                kind="error",
                label="Run 失败",
                status="failed",
                context_anchor_id=anchor_id,
                output_context_anchor_id=anchor_id,
                raw_event_ids=[event.event_id],
                error=event.error,
            )
        )

    def _append_llm_output(self, event: TraceLogEvent, node_id: str) -> str:
        messages = _output_messages(event.output)
        tool_call_names = _tool_call_names(event.tool_calls)
        output_metadata = _llm_metadata(event) | _phase_metadata("output", "llm_end", status=event.status, model_name=event.model_name)
        last_anchor_id = ""
        if not messages:
            return self._append_context(
                event,
                kind="ai",
                title=_message_title("ai", event),
                content=_llm_content(event.output, tool_call_names),
                metadata=output_metadata,
                tool_call_names=tool_call_names,
                related_node_id=node_id,
            )
        for message in messages:
            kind = _message_kind(message)
            if kind != "ai":
                continue
            message_tool_call_names = _message_tool_call_names(message)
            names = message_tool_call_names or tool_call_names
            content = _llm_content(_message_content(message), names)
            if _is_empty_content(content):
                continue
            last_anchor_id = self._append_context(
                event,
                kind="ai",
                title=_message_title("ai", event),
                content=content,
                metadata=_message_metadata(message) | output_metadata,
                tool_call_names=names,
                related_node_id=node_id,
            )
        if not last_anchor_id:
            return self._append_context(
                event,
                kind="ai",
                title=_message_title("ai", event),
                content=_llm_content(event.output, tool_call_names),
                metadata=output_metadata,
                tool_call_names=tool_call_names,
                related_node_id=node_id,
            )
        return last_anchor_id

    def _append_tool_output(self, event: TraceLogEvent, node_id: str) -> str:
        status = _tool_status(event.tool_output) or event.status
        return self._append_context(
            event,
            kind="tool",
            title=f"Tool 输出 · {event.tool_name or 'Tool'}",
            content=_tool_content(event.tool_output),
            metadata=_phase_metadata("output", "tool_end", status=status, duration_ms=event.duration_ms, tool_name=event.tool_name),
            related_node_id=node_id,
        )

    def _append_todo(self, end: TraceLogEvent, start: TraceLogEvent | None, tool_anchor_id: str) -> None:
        items = _todo_items(end.tool_output)
        if not items:
            return
        todo_node_id = f"todo:{end.event_id}"
        anchor_id = self._append_context(
            end,
            kind="todo",
            title="Todo 更新",
            content=[item.model_dump() for item in items],
            metadata={"phase": "output", "tool_anchor_id": tool_anchor_id, "tool_name": end.tool_name},
            related_node_id=todo_node_id,
        )
        snapshot = TraceTodoSnapshot(
            anchor_id=anchor_id,
            agent_name=end.agent_name,
            items=items,
            active_item=_active_todo(items),
        )
        self.projection.todos.append(snapshot)
        self.projection.nodes.append(
            TraceNode(
                node_id=todo_node_id,
                parent_node_id=_agent_node_id(end),
                kind="todo",
                label="Todo 更新",
                status=end.status,
                agent_name=end.agent_name,
                agent_role=_agent_role(end.agent_name),
                depth=_agent_depth(end.agent_name),
                started_at=start.timestamp if start else None,
                ended_at=end.timestamp,
                duration_ms=end.duration_ms,
                context_anchor_id=anchor_id,
                output_context_anchor_id=anchor_id,
                raw_event_ids=_raw_ids(start, end),
            )
        )

    def _append_error(self, event: TraceLogEvent, node_id: str, title: str) -> str:
        return self._append_context(
            event,
            kind="error",
            title=title,
            content=event.error or _tool_error(event.tool_output) or event.output,
            metadata=_phase_metadata("error", event.type, status="failed", duration_ms=event.duration_ms, model_name=event.model_name, tool_name=event.tool_name),
            related_node_id=node_id,
        )

    def _append_context(
        self,
        event: TraceLogEvent,
        *,
        kind: str,
        title: str,
        content: Any,
        metadata: dict[str, Any] | None = None,
        tool_call_names: list[str] | None = None,
        related_node_id: str | None = None,
        collapsed_by_default: bool = False,
    ) -> str:
        self.context_sequence += 1
        anchor_id = f"ctx-{event.sequence}-{self.context_sequence}"
        segment = TraceContextSegment(
            anchor_id=anchor_id,
            sequence=self.context_sequence,
            kind=kind,  # type: ignore[arg-type]
            agent_name=event.agent_name,
            agent_role=_agent_role(event.agent_name),
            depth=_agent_depth(event.agent_name),
            title=title,
            content=content,
            metadata={key: value for key, value in (metadata or {}).items() if value is not None},
            tool_call_names=tool_call_names or [],
            related_node_id=related_node_id,
            collapsed_by_default=collapsed_by_default,
        )
        self.projection.context.append(segment)
        return anchor_id


def _run_node(run: TraceRunSummary, events: list[TraceLogEvent]) -> TraceNode:
    return TraceNode(
        node_id="run",
        kind="run",
        label=run.endpoint,
        status=run.status,
        started_at=run.started_at,
        ended_at=run.ended_at,
        duration_ms=run.duration_ms,
        raw_event_ids=[event.event_id for event in events if event.type in {"run_start", "run_end", "run_error"}],
        error=run.error,
    )


def _pop_pending(pending: dict[str, list[T]], key: str) -> T | None:
    values = pending.get(key)
    if not values:
        return None
    item = values.pop(0)
    if not values:
        del pending[key]
    return item


def _event_pair_key(event: TraceLogEvent) -> str:
    if event.tool_call_id:
        return f"tool_call:{event.tool_call_id}"
    if event.run_id:
        return f"run:{event.run_id}"
    if event.type.startswith("llm"):
        return f"llm:{event.agent_name or 'unknown'}:{event.model_name or 'unknown'}"
    if event.type.startswith("tool"):
        return f"tool:{event.agent_name or 'unknown'}:{event.tool_name or 'unknown'}"
    return f"event:{event.agent_name or 'unknown'}"


def _raw_ids(*events: TraceLogEvent | None) -> list[str]:
    return [event.event_id for event in events if event is not None]


def _llm_node_id(event: TraceLogEvent) -> str:
    return f"llm:{event.event_id}"


def _tool_node_id(event: TraceLogEvent) -> str:
    return f"tool:{event.event_id}"


def _range_start(value: TraceContextRange | None) -> str | None:
    return value.start_anchor_id if value else None


def _phase_metadata(phase: str, event_type: str, **values: Any) -> dict[str, Any]:
    return {"phase": phase, "event_type": event_type, **{key: value for key, value in values.items() if value is not None}}


def _agent_node_id(event: TraceLogEvent) -> str:
    if not event.agent_name:
        return "run"
    return f"agent:{event.agent_name}"


def _agent_role(agent_name: str | None) -> str | None:
    if agent_name is None:
        return None
    if agent_name.endswith("-subagent"):
        return "subagent"
    return "main"


def _agent_depth(agent_name: str | None) -> int:
    return 1 if _agent_role(agent_name) == "subagent" else 0


def _llm_label(event: TraceLogEvent) -> str:
    return f"LLM · {event.model_name}" if event.model_name else "LLM 调用"


def _tool_label(event: TraceLogEvent) -> str:
    return f"Tool · {event.tool_name}" if event.tool_name else "Tool 调用"


def _iter_messages(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _output_messages(output: Any) -> list[Any]:
    if isinstance(output, dict):
        return _iter_messages(output.get("messages"))
    return []


def _message_kind(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("type") or message.get("role") or "")
    return ""


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return message


def _message_metadata(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    return {
        key: value
        for key, value in message.items()
        if key not in {"content", "type", "role"} and value not in (None, "", [], {})
    }


def _message_tool_call_names(message: Any) -> list[str]:
    if not isinstance(message, dict):
        return []
    return _tool_call_names(message.get("tool_calls"))


def _message_title(kind: str, event: TraceLogEvent) -> str:
    if kind == "human":
        return "用户输入"
    if kind == "ai":
        return event.agent_name or "AI 输出"
    if kind == "tool":
        return event.tool_name or "工具返回"
    return kind


def _llm_content(content: Any, tool_call_names: list[str]) -> Any:
    if tool_call_names and _is_empty_content(content):
        return "调用工具中..."
    return content


def _is_empty_content(content: Any) -> bool:
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list | dict):
        return not content
    return False


def _tool_call_names(tool_calls: Any) -> list[str]:
    if not isinstance(tool_calls, list):
        return []
    names: list[str] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        name = tool_call.get("name")
        if not isinstance(name, str):
            continue
        stripped = name.strip()
        if stripped:
            names.append(stripped)
    return names


def _llm_metadata(event: TraceLogEvent) -> dict[str, Any]:
    metadata: dict[str, Any] = {"model_name": event.model_name, "duration_ms": event.duration_ms}
    usage = event.usage or _usage_from_llm_output(event.output)
    if usage is not None:
        metadata["usage"] = usage.model_dump(exclude_none=True)
    if event.tool_calls is not None:
        metadata["tool_calls"] = event.tool_calls
    return {key: value for key, value in metadata.items() if value is not None}


def _usage_from_llm_output(output: Any) -> TraceUsage | None:
    if not isinstance(output, dict):
        return None
    messages = output.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        usage = message.get("usage") or message.get("usage_metadata")
        if isinstance(usage, dict):
            return TraceUsage(
                input_tokens=_int_or_none(usage.get("input_tokens") or usage.get("prompt_tokens")),
                output_tokens=_int_or_none(usage.get("output_tokens") or usage.get("completion_tokens")),
                total_tokens=_int_or_none(usage.get("total_tokens")),
            )
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tool_content(output: Any) -> Any:
    if isinstance(output, dict) and "content" in output:
        return output["content"]
    return output


def _tool_status(output: Any) -> str | None:
    if isinstance(output, dict):
        status = output.get("status")
        return str(status) if status else None
    return None


def _tool_error(output: Any) -> str | None:
    if isinstance(output, dict) and output.get("status") == "error":
        content = output.get("content")
        return str(content) if content is not None else "Tool returned error status."
    return None


def _todo_items(tool_output: Any) -> list[TraceTodoItem]:
    todos = _find_todos(tool_output)
    if todos is None:
        return []
    return [_todo_item(item, index) for index, item in enumerate(todos)]


def _find_todos(value: Any) -> list[Any] | None:
    if isinstance(value, dict):
        if isinstance(value.get("todos"), list):
            return value["todos"]
        update = value.get("update")
        if isinstance(update, dict) and isinstance(update.get("todos"), list):
            return update["todos"]
        args = value.get("args")
        if isinstance(args, dict) and isinstance(args.get("todos"), list):
            return args["todos"]
    return None


def _todo_item(item: Any, index: int) -> TraceTodoItem:
    if not isinstance(item, dict):
        return TraceTodoItem(id=str(index + 1), content=str(item), status="pending")
    content = item.get("content")
    if content is None:
        content = item.get("title")
    status = item.get("status")
    if status not in {"pending", "in_progress", "completed"}:
        status = "pending"
    item_id = item.get("id")
    return TraceTodoItem(id=str(item_id) if item_id is not None else str(index + 1), content=str(content), status=status)


def _active_todo(items: list[TraceTodoItem]) -> str | None:
    for item in items:
        if item.status == "in_progress":
            return item.content
    return None
