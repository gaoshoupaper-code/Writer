// ── Trace 类型（搬迁自 evolution/frontend/lib/types.ts，仅保留 trace 相关 + 新增监测类型）──

export type TraceStatus = "running" | "awaiting_input" | "completed" | "failed" | "cancelled";

export type TraceEventType =
  | "run_start"
  | "run_end"
  | "run_error"
  | "run_awaiting"
  | "run_cancelled"
  | "llm_start"
  | "llm_end"
  | "llm_error"
  | "tool_start"
  | "tool_end"
  | "tool_error";

export type TraceUsage = {
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
};

export type TraceContextRange = {
  start_anchor_id?: string | null;
  end_anchor_id?: string | null;
};

export type TraceNodeKind = "run" | "agent" | "llm" | "tool" | "todo" | "error" | "skill" | (string & {});
export type TraceAgentRole = "main" | "subagent" | (string & {});
export type TraceContextKind = "system" | "human" | "ai" | "tool" | "todo" | "error" | "skill" | (string & {});

export type TraceLogEvent = {
  trace_id: string;
  event_id: string;
  sequence: number;
  type: TraceEventType;
  status: TraceStatus;
  timestamp: string;
  source: "system" | "middleware";
  duration_ms?: number | null;
  run_id?: string | null;
  parent_run_id?: string | null;
  parent_event_id?: string | null;
  agent_name?: string | null;
  node_name?: string | null;
  model_name?: string | null;
  input?: unknown;
  output?: unknown;
  usage?: TraceUsage | null;
  tool_calls?: unknown;
  tool_call_id?: string | null;
  tool_name?: string | null;
  tool_args?: unknown;
  tool_output?: unknown;
  context_anchor_id?: string | null;
  input_context_range?: TraceContextRange | null;
  output_context_anchor_id?: string | null;
  error?: string | null;
  skill_name?: string | null;
};

export type TraceNode = {
  node_id: string;
  parent_node_id?: string | null;
  kind: TraceNodeKind;
  label: string;
  status: TraceStatus;
  agent_name?: string | null;
  agent_role?: TraceAgentRole | null;
  depth: number;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  model_name?: string | null;
  tool_name?: string | null;
  skill_name?: string | null;
  usage?: TraceUsage | null;
  context_anchor_id?: string | null;
  input_context_range?: TraceContextRange | null;
  output_context_anchor_id?: string | null;
  raw_event_ids: string[];
  error?: string | null;
  chain_summary?: string | null;
  parallel_group_id?: string | null;
};

export type TraceContextSegment = {
  anchor_id: string;
  sequence: number;
  kind: TraceContextKind;
  agent_name?: string | null;
  agent_role?: TraceAgentRole | null;
  depth: number;
  title: string;
  content: unknown;
  metadata: Record<string, unknown>;
  tool_call_names: string[];
  related_node_id?: string | null;
  collapsed_by_default: boolean;
};

export type TraceTodoItem = {
  id?: string | null;
  content: string;
  status: "pending" | "in_progress" | "completed";
};

export type TraceTodoSnapshot = {
  anchor_id: string;
  agent_name?: string | null;
  items: TraceTodoItem[];
  active_item?: string | null;
};

export type TraceRunSummary = {
  trace_id: string;
  workspace_id: string;
  thread_id: string;
  session_name: string;
  workspace_path: string;
  endpoint: string;
  status: TraceStatus;
  started_at: string;
  ended_at?: string | null;
  duration_ms?: number | null;
  event_count: number;
  path: string;
  error?: string | null;
};

export type TraceDetail = {
  run: TraceRunSummary;
  events: TraceLogEvent[];
  nodes: TraceNode[];
  context: TraceContextSegment[];
  todos: TraceTodoSnapshot[];
};

/**
 * 详情接口轻量返回（Phase 2：去 events/context，前端按需懒加载）。
 * events 和 context 不再全量返回，前端打开抽屉时调懒加载接口拉取。
 */
export type TraceDetailLite = {
  run: TraceRunSummary;
  nodes: TraceNode[];
  todos: TraceTodoSnapshot[];
};

/**
 * SSE node patch（Phase 2 T5：evolution 源推送的增量 node 变更）。
 * 前端收到后做 append（新 node）/ update（按 node_id 覆盖）。
 */
export type NodePatch = {
  appended: TraceNode[];
  updated: TraceNode[];
};

/**
 * SSE 统一信封（Phase 2 T5：按 source 分流）。
 * evolution 源推 node patch，executor 源推原始 event。
 */
export type SseEnvelope = {
  _type: "data" | "snapshot" | "end" | "error";
  source?: "evolution" | "executor";
  data: unknown;
};

// ── 监测端新增类型 ──

/** trace 列表项（evolution /api/traces 返回，对应 TraceListItem） */
export type TraceListItem = {
  trace_id: string;
  workspace_id: string;
  thread_id: string | null;
  session_name: string | null;
  endpoint: string | null;
  status: TraceStatus;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  event_count: number;
  error: string | null;
  flag_count: number;
  owner_user_id: string;
  run_purpose: string;
};

/** 活跃 trace（evolution /api/active-runs 富化返回，D7） */
export type ActiveRun = {
  trace_id: string;
  workspace_id: string;
  thread_id: string | null;
  endpoint: string | null;
  status: TraceStatus;
  started_at: string | null;
  duration_ms: number | null;
  event_count: number;
  // D7 富化字段（join evolution.db，未摄入时为 null 降级）
  session_name: string | null;
  ingested: boolean;
  /** trace 来源（D9 后端补齐：user_generation / evolution_eval / evolution_evolve） */
  run_purpose: string | null;
};

// ── 宏观统计（/api/stats/*）──

export type StatsOverview = {
  total: number;
  success: number;
  failed: number;
  error_rate: number;
  duration_p50: number | null;
  duration_p90: number | null;
  duration_p99: number | null;
  total_tokens: number;
  total_input_tokens: number;
  total_output_tokens: number;
};

export type SkillStat = {
  agent_name: string;
  call_count: number;
  node_count: number;
  avg_duration_ms: number | null;
  fail_count: number;
  fail_rate: number;
};

export type TimelinePoint = {
  bucket: string;
  total: number;
  failed: number;
};

export type FailurePattern = {
  error_pattern: string;
  count: number;
  sample_trace_ids: string[];
};
