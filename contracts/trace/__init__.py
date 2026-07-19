"""trace 数据 schema —— 执行端与进化端的共享契约（单一真源）。

执行端 recorder 按 schema 写 trace jsonl，进化端 ingestion/loader/projector 按 schema 读。
修改这里的字段定义会同时影响两端，改前想清楚。

字段说明中的 anchor（锚点）：recorder 为每条事件分配的稳定 ID，写进 jsonl 后永久不变。
进化端摄入时直接读用，重建上下文时顺着 anchor 链回溯。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TraceStatus = Literal["running", "awaiting_input", "completed", "failed", "cancelled", "interrupted"]
TraceEventType = Literal[
    "run_start",
    "run_end",
    "run_error",
    "run_meta",
    "run_awaiting",
    "run_cancelled",
    "llm_start",
    "llm_end",
    "llm_error",
    "tool_start",
    "tool_end",
    "tool_error",
    # 数据闭环 E（隐式反馈信号）：用户行为埋点，promote 闸门判质量用。
    # copy = 用户复制了内容（正信号）；regenerate = 用户点了重试（负信号）。
    "user_copy",
    "user_regenerate",
]
TraceNodeKind = Literal["run", "agent", "llm", "tool", "todo", "error", "skill"]
TraceAgentRole = Literal["main", "subagent"]
TraceContextKind = Literal["system", "human", "ai", "tool", "todo", "error", "skill"]
TraceTodoStatus = Literal["pending", "in_progress", "completed"]


class TraceUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class TraceMemoryQuality(BaseModel):
    """记忆系统检索质量埋点（P4 进化闭环）。

    memory_recall middleware 每次 before_model 检索后记录一条。
    evolution 侧读 trace 的 run_meta 事件提取此字段，归纳记忆失败模式。

    字段来自设计方案 §7.3 扩展 1（记忆质量 trace 维度）。
    """
    chapter_num: int | None = None           # 当前写第几章（查询时的章节号）
    query: str = ""                          # 实际检索查询（截断前 200 字）
    evidence_packet_tokens: int = 0          # 注入的证据包估算 token 数
    evidence_nodes_count: int = 0            # 命中的图节点数
    evidence_edges_count: int = 0            # 命中的关系边数
    retrieval_ok: bool = True                # 检索是否成功（False = 异常/图谱为空）
    error: str | None = None                 # 检索失败时的错误信息


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
    # trace 稳定性重构（设计 20260720_203000）：Pull 主导架构下 runs.status 是唯一真相源，
    # 心跳字段供前端判断"还在跑"，interrupted_reason 供排查中断来源。
    # 可选字段：executor 端构造时不传即 None，向后兼容。
    last_heartbeat_at: str | None = None
    interrupted_reason: str | None = None


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
    # 增量存储锚点：recorder 为每条事件分配的稳定 anchor_id。
    # 写进 jsonl 永久稳定，进化端摄入时直接读用，重建上下文时顺着 anchor 链回溯。
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
