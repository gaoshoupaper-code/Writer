"""trace 数据 schema —— 执行端与进化端的共享契约（单一真源）。

执行端 recorder 按 schema 写 trace jsonl，进化端 ingestion/loader/projector 按 schema 读。
修改这里的字段定义会同时影响两端，改前想清楚。

字段说明中的 anchor（锚点）：recorder 为每条事件分配的稳定 ID，写进 jsonl 后永久不变。
进化端摄入时直接读用，重建上下文时顺着 anchor 链回溯。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

# ── 四维正交生命周期契约（DEC-008）──
# 维度1 业务状态：覆盖 pending→running→cancelling→终态 的主路径与 cancel_timeout→cancelled 恢复边。
#   awaiting_input 是 HITL 挂起态（running 的变体），与新增的取消收敛态正交。
TraceStatus = Literal[
    "pending", "running", "awaiting_input",
    "cancelling",  # 已受理停止、执行收敛中（立即可见中间态，FR-006）
    "completed", "failed", "evidence_capture_failed",
    "cancelled", "cancel_timeout",  # 取消类终态（cancel_timeout 仅可恢复为 cancelled）
    "interrupted",  # 无存活 owner 的历史/失联中断态（FR-009）
]
TraceWorkload = Literal["creation", "evidence_compile", "evaluation", "evolution"]
# 维度3 完整性：recording/sealing 期间为 pending（不可与 verified/incomplete 终态混用，FR-008/CON-007）。
TraceIntegrityStatus = Literal["pending", "verified", "incomplete", "conflict", "legacy"]
# 维度2 Trace 记录阶段：recording→sealing→{sealed|degraded}，与业务状态、完整性正交（FR-008）。
TracePhase = Literal["recording", "sealing", "sealed", "degraded"]
TraceCoverageStatus = Literal["known", "partial", "unknown", "not_applicable"]
TracePayloadKind = Literal["semantic_full", "reference_only", "structural"]
TraceEventType = Literal[
    "run_start",
    "run_end",
    "run_error",
    "run_meta",
    "run_awaiting",
    # 维度4 取消时间线（FR-006）：request/accept 让"用户取消"在时间线可见，
    # run_cancelled 是确认收敛终态，cancel_timeout 是 10s 内无法确认收敛的诚实告警态。
    "cancel_requested",
    "cancel_accepted",
    "run_cancelled",
    "cancel_timeout",
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
    "span_link",
    "skill_catalog",
    "skill_activation",
    "middleware_assembly",
    "middleware_intervention",
    "hitl",
    "artifact_revision",
    "capture_degraded",
    "run_manifest",
]
TraceNodeKind = Literal["run", "agent", "llm", "tool", "todo", "error", "skill", "middleware", "artifact", "hitl"]
TraceAgentRole = Literal["main", "subagent"]
TraceContextKind = Literal["system", "human", "ai", "tool", "todo", "error", "skill"]
TraceTodoStatus = Literal["pending", "in_progress", "completed"]


class TraceUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class TracePayloadRef(BaseModel):
    """受治理正文的不可变引用；事件不得内联持久化语义正文。"""

    payload_id: str
    content_hash: str
    kind: TracePayloadKind
    size_bytes: int
    sensitivity: Literal["internal", "restricted"] = "restricted"
    expires_at: str | None = None


class TraceSpanLink(BaseModel):
    """异步因果关系，不伪造为跨 Trace 父子 Span。"""

    target_trace_id: str
    target_span_id: str | None = None
    relation: Literal["triggered_by", "retry_of", "resumed_from", "derived_from", "consumes", "produces"]
    artifact_revision_id: str | None = None
    artifact: dict[str, Any] | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceManifest(BaseModel):
    """executor 终态落盘清单，供 evolution 校验连续摄入。"""

    schema_version: int = 2
    trace_id: str
    final_sequence: int
    terminal_event_id: str
    events_hash: str
    payload_ids: list[str] = Field(default_factory=list)
    capture_degraded: bool = False
    created_at: str


class CancelAudit(BaseModel):
    """取消身份与收敛审计（维度4，FR-006/CON-003/DEC-008）。

    一次用户停止请求对应一个稳定幂等的 cancel_id，跨 executor/evolution/桌面贯穿，
    保证取消意图不因子进程强杀、服务重启、页面关闭或 trace_id 尚未产生而丢失。
    converge_status 记录最终收敛结果（cancelled/cancel_timeout），让取消时间线可审计。
    全部字段可空：未发起取消的 trace 为 None；部分字段在收敛完成前缺失。
    """

    cancel_id: str
    requested_by: str | None = None
    requested_at: str | None = None
    reason: str | None = None
    accepted_at: str | None = None
    converged_at: str | None = None
    converge_status: Literal["cancelled", "cancel_timeout"] | None = None


class SkillCatalogEntry(BaseModel):
    skill_id: str
    name: str
    version: str | None = None
    content_hash: str
    source: str
    scope: str
    runtime_path: str


class MiddlewareDescriptor(BaseModel):
    name: str
    position: int
    config_hash: str
    version: str | None = None
    mount_location: str = "agent.custom_middleware"


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
    # V2：新 trace 显式写 2；旧索引和历史 JSONL 仍按默认 1 读取。
    schema_version: int = 1
    service: str | None = None
    workload: TraceWorkload | None = None
    purpose: str | None = None
    integrity_status: TraceIntegrityStatus = "legacy"
    coverage: dict[str, TraceCoverageStatus] = Field(default_factory=dict)
    run_snapshot: dict[str, Any] = Field(default_factory=dict)
    links: list[TraceSpanLink] = Field(default_factory=list)
    external_refs: dict[str, str] = Field(default_factory=dict)
    manifest: TraceManifest | None = None
    # 四维正交生命周期（DEC-008，全部向后兼容默认值）：
    #   trace_phase —— 记录/封存阶段，与业务状态、完整性正交；旧索引缺省 None。
    #   cancel_audit —— 取消身份审计（CancelAudit），未取消即 None。
    #   lifecycle_revision —— 单调递增，桌面据此拒绝旧快照覆盖新状态（CON-006）；旧索引缺省 0。
    trace_phase: TracePhase | None = None
    cancel_audit: CancelAudit | None = None
    lifecycle_revision: int = 0

    @property
    def trace_incomplete(self) -> bool:
        """下游可信消费门禁（CON-001/FR-004）。

        pending（记录/封存中、尚未校验）既不是完整也不是损坏——下游继续关闭，
        但 UI 不得把它当终态 incomplete 展示（FR-008/AC-010）。仅 verified 放行，
        仅非 pending 的非 verified 才算真"不完整"。
        """
        return self.integrity_status not in ("verified", "pending")


class TraceLogEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    trace_id: str
    event_id: str
    sequence: int
    type: TraceEventType
    status: TraceStatus
    timestamp: str
    source: Literal["system", "middleware", "runtime"]
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
    schema_version: int = 1
    span_id: str | None = None
    payload_refs: dict[str, TracePayloadRef] = Field(default_factory=dict)
    links: list[TraceSpanLink] = Field(default_factory=list)
    skill_catalog: list[SkillCatalogEntry] = Field(default_factory=list)
    skill_activation: dict[str, Any] | None = None
    middleware_stack: list[MiddlewareDescriptor] = Field(default_factory=list)
    intervention: dict[str, Any] | None = None
    hitl: dict[str, Any] | None = None
    artifact_revision_id: str | None = None
    artifact: dict[str, Any] | None = None


def compute_trace_events_hash(events: Iterable[TraceLogEvent]) -> str:
    """Return the canonical digest shared by manifests and receipts."""
    digest = hashlib.sha256()
    for event in sorted(events, key=lambda item: item.sequence):
        canonical_event = json.dumps(
            event.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest.update(canonical_event.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


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
