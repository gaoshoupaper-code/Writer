from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TraceStatus = Literal["running", "completed", "failed"]
TraceEventType = Literal[
    "run_start",
    "run_end",
    "run_error",
    "run_meta",
    "llm_start",
    "llm_end",
    "llm_error",
    "tool_start",
    "tool_end",
    "tool_error",
]
TraceNodeKind = Literal["run", "agent", "llm", "tool", "todo", "error", "skill"]
TraceAgentRole = Literal["main", "subagent"]
TraceContextKind = Literal["system", "human", "ai", "tool", "todo", "error", "skill"]
TraceTodoStatus = Literal["pending", "in_progress", "completed"]


class TraceUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class TraceContextRange(BaseModel):
    start_anchor_id: str | None = None
    end_anchor_id: str | None = None


class TraceRunSummary(BaseModel):
    trace_id: str
    workspace_id: str
    thread_id: str
    session_name: str
    workspace_path: str
    endpoint: str
    status: TraceStatus
    started_at: str
    ended_at: str | None = None
    duration_ms: int | None = None
    event_count: int = 0
    path: str
    error: str | None = None


class TraceLogEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    trace_id: str
    event_id: str
    sequence: int
    type: TraceEventType
    status: TraceStatus
    timestamp: str
    source: Literal["system", "middleware"]
    duration_ms: int | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    parent_event_id: str | None = None
    agent_name: str | None = None
    node_name: str | None = None
    model_name: str | None = None
    input: Any | None = None
    output: Any | None = None
    usage: TraceUsage | None = None
    tool_calls: Any | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_args: Any | None = None
    tool_output: Any | None = None
    context_anchor_id: str | None = None
    input_context_range: TraceContextRange | None = None
    output_context_anchor_id: str | None = None
    # 增量存储锚点（Phase 1 T1）：recorder 为每条事件分配的稳定 anchor_id。
    # 写进 jsonl 永久稳定，monitoring 摄入时直接读用，重建时顺着 anchor 链回溯。
    output_anchor_id: str | None = None
    error: str | None = None
    skill_name: str | None = None


class TraceNode(BaseModel):
    node_id: str
    parent_node_id: str | None = None
    kind: TraceNodeKind
    label: str
    status: TraceStatus
    agent_name: str | None = None
    agent_role: TraceAgentRole | None = None
    depth: int = 0
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    model_name: str | None = None
    tool_name: str | None = None
    skill_name: str | None = None
    usage: TraceUsage | None = None
    context_anchor_id: str | None = None
    input_context_range: TraceContextRange | None = None
    output_context_anchor_id: str | None = None
    raw_event_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    chain_summary: str | None = None
    parallel_group_id: str | None = None


class TraceContextSegment(BaseModel):
    anchor_id: str
    sequence: int
    kind: TraceContextKind
    agent_name: str | None = None
    agent_role: TraceAgentRole | None = None
    depth: int = 0
    title: str
    content: Any
    metadata: dict[str, Any] = Field(default_factory=dict)
    tool_call_names: list[str] = Field(default_factory=list)
    related_node_id: str | None = None
    collapsed_by_default: bool = False


class TraceTodoItem(BaseModel):
    id: str | None = None
    content: str
    status: TraceTodoStatus


class TraceTodoSnapshot(BaseModel):
    anchor_id: str
    agent_name: str | None = None
    items: list[TraceTodoItem] = Field(default_factory=list)
    active_item: str | None = None


class TraceDetail(BaseModel):
    run: TraceRunSummary
    events: list[TraceLogEvent] = Field(default_factory=list)
    nodes: list[TraceNode] = Field(default_factory=list)
    context: list[TraceContextSegment] = Field(default_factory=list)
    todos: list[TraceTodoSnapshot] = Field(default_factory=list)
