"""trace jsonl 读取器：解析 jsonl → TraceLogEvent 列表。

复刻后端 recorder 的 _read_events 逻辑（含 _sanitize_event_data + run_link 过滤），
确保 evolution 读出的 events 与后端运行时投影的输入一致。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.core.models import TraceLogEvent


def read_events(trace_path: Path) -> list[TraceLogEvent]:
    """读取一个 trace jsonl 文件，返回按 (timestamp, sequence) 排序的事件列表。"""
    events_by_id: dict[str, TraceLogEvent] = {}
    with trace_path.open("r", encoding="utf-8") as file:
        for line in file:
            stripped = line.strip()
            if not stripped:
                continue
            event_data = json.loads(stripped)
            # 跳过 callback 的 run_link 事件（与后端 _read_events 一致）
            if event_data.get("type") == "run_link" and event_data.get("source") == "callback":
                continue
            event = TraceLogEvent.model_validate(_sanitize_event_data(event_data))
            events_by_id.setdefault(event.event_id, event)
    return sorted(events_by_id.values(), key=lambda event: (event.timestamp, event.sequence))


# ── event 数据清洗（复刻后端 recorder._sanitize_event_data）──


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
