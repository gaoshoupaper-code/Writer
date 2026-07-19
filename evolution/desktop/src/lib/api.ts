import { invoke } from "@tauri-apps/api/core";
import type {
  TraceDetailLite,
  TraceListItem,
  TraceListResponse,
  UserCacheItem,
  ActiveRun,
  StatsOverview,
  SkillStat,
  TimelinePoint,
  FailurePattern,
  TraceLogEvent,
  TraceContextSegment,
} from "@/lib/types";

// 统一类型从 lib/types.ts 引用（trace 移植后对齐）
export type {
  TraceDetailLite,
  TraceListItem,
  TraceListResponse,
  UserCacheItem,
  ActiveRun,
  StatsOverview,
  SkillStat,
  TimelinePoint,
  FailurePattern,
};

/**
 * Evolution 桌面端 API 客户端（桌面化改造 2026-07-07）。
 *
 * 复用写作 desktop 的 Rust 中继架构（apiFetch + RelayResponse + onUnauthorized），
 * 业务函数从零写（evolution 领域）。
 *
 * 关键差异：evolution 后端通过 nginx 子路径 /evolution-api 反代，
 * 所以所有请求 path 前加 "/evolution-api" 前缀。
 * 登录走 executor 的 /api（SSO），不加 evolution-api 前缀。
 */

/// Rust http_request command 的返回结构（对应 src-tauri/src/http.rs HttpResponse）。
interface HttpRelayResponse {
  status: number;
  headers: Record<string, string>;
  body: string;
}

/**
 * 401 跳转回调：由路由层注入（api.ts 不直接耦合 react-router）。
 * 去抖：500ms 窗口内多次 401 只触发一次，避免并发请求同时 401 导致反复跳转闪烁。
 */
let onUnauthorized: (() => void) | null = null;
let _unauthorizedFiring: boolean = false;

export function setUnauthorizedHandler(handler: (() => void) | null) {
  onUnauthorized = handler;
}

function triggerUnauthorized() {
  if (!onUnauthorized || _unauthorizedFiring) return;
  _unauthorizedFiring = true;
  onUnauthorized();
  // 500ms 去抖窗口：窗口内到达的其它 401 不再重复触发跳转
  setTimeout(() => { _unauthorizedFiring = false; }, 500);
}

/**
 * 仿 Response 对象：让上层业务代码用 .ok/.status/.json() 无需改动。
 */
class RelayResponse {
  readonly status: number;
  readonly headers: Map<string, string>;
  private readonly bodyText: string;

  constructor(relay: HttpRelayResponse) {
    this.status = relay.status;
    this.headers = new Map(Object.entries(relay.headers));
    this.bodyText = relay.body;
  }

  get ok(): boolean {
    return this.status >= 200 && this.status < 300;
  }

  async json(): Promise<any> {
    return JSON.parse(this.bodyText);
  }

  async text(): Promise<string> {
    return this.bodyText;
  }
}

// evolution API 前缀：
// - 生产：/evolution-api（nginx 子路径反代，同域 cookie 共享 SSO）
// - 本地 dev：空前缀（直连 evolution:7789，无 nginx）
const EVO_PREFIX = import.meta.env.DEV ? "" : "/evolution-api";

/**
 * 统一请求封装（走 Rust 中继）：
 * - executor 接口（登录/鉴权）：path 不加前缀（如 /api/auth/login）
 * - evolution 接口：path 加 /evolution-api 前缀（如 /evolution-api/api/config/llm）
 *
 * 用 evoFetch 调 evolution，apiFetch 调 executor。
 */
export async function apiFetch(input: string, init: RequestInit = {}): Promise<RelayResponse> {
  return _fetch(input, init);
}

/** evolution 接口专用（自动加 /evolution-api 前缀）。 */
export async function evoFetch(path: string, init: RequestInit = {}): Promise<RelayResponse> {
  // path 形如 "/api/config/llm"，加前缀后 "/evolution-api/api/config/llm"
  const full = path.startsWith("/") ? `${EVO_PREFIX}${path}` : `${EVO_PREFIX}/${path}`;
  return _fetch(full, init);
}

/**
 * 503（认证服务不可达）静默重试：最多 3 次，指数退避 1s/2s/4s。
 * 重试期间不跳登录、不闪烁，只有重试耗尽才把 503 响应返回给上层。
 */
const _RETRY_503_MAX = 3;
const _RETRY_503_BASE_MS = 1000;

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function _fetch(input: string, init: RequestInit): Promise<RelayResponse> {
  let bodyValue: unknown = undefined;
  if (init.body) {
    const raw = typeof init.body === "string" ? init.body : String(init.body);
    try {
      bodyValue = JSON.parse(raw);
    } catch {
      bodyValue = raw;
    }
  }

  const headers: Record<string, string> = {};
  if (init.headers) {
    const h = init.headers as Record<string, string>;
    for (const k of Object.keys(h)) {
      headers[k] = h[k];
    }
  }

  const reqArgs = {
    path: input,
    method: init.method || "GET",
    headers: Object.keys(headers).length ? headers : null,
    body: bodyValue ?? null,
    stream: false,
  };

  // 503 重试循环：executor 不可达时静默重试，不跳登录
  for (let attempt = 0; ; attempt++) {
    const relay = await invoke<HttpRelayResponse>("http_request", { request: reqArgs });

    if (relay.status === 503 && attempt < _RETRY_503_MAX) {
      await sleep(_RETRY_503_BASE_MS * Math.pow(2, attempt)); // 1s/2s/4s
      continue;
    }

    // 401 → 触发跳登录（去抖：500ms 窗口内多次只触发一次）
    if (relay.status === 401) {
      triggerUnauthorized();
    }

    return new RelayResponse(relay);
  }
}

/** JSON 解析辅助：非 2xx 抛错，2xx 返回 json。 */
async function apiJson<T>(input: string, init: RequestInit, fetcher = apiFetch): Promise<T> {
  const resp = await fetcher(input, init);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(text || `HTTP ${resp.status}`);
  }
  return resp.json();
}

/** evolution JSON 辅助（加前缀）。 */
async function evoJson<T>(path: string, init: RequestInit): Promise<T> {
  return apiJson<T>(path, init, evoFetch);
}

// ════════════════════════════════════════════════════════════
//  认证（走 executor /api，SSO）
// ════════════════════════════════════════════════════════════

export interface AuthMe {
  user_id: string;
  username: string;
  is_admin: boolean;
  is_super_admin: boolean;
  has_api_key: boolean;
}

export async function login(username: string, password: string): Promise<AuthMe> {
  return apiJson<AuthMe>("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
}

export async function logout(): Promise<{ ok: boolean }> {
  return apiJson<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
}

/** 探测登录态（非抛错版，路由守卫用）。 */
export async function fetchMeOrNull(): Promise<AuthMe | null> {
  try {
    const resp = await apiFetch("/api/auth/me");
    if (!resp.ok) return null;
    return resp.json();
  } catch {
    return null;
  }
}

// ════════════════════════════════════════════════════════════
//  LLM 配置（走 evolution /evolution-api/api/config）
//  scope 分家（2026-07-18）：'evolution'=评估 / 'executor'=写作，各自独立激活
// ════════════════════════════════════════════════════════════

/** LLM 配置归属 scope：进化 Agent 评估用 / executor 写作用。 */
export type LlmConfigScope = "evolution" | "executor";

export interface LlmConfigOut {
  has_key: boolean;
  name: string | null;
  base_url: string;
  model: string;
  updated_at: string | null;
}

/** 配置列表项（不回显 key 明文，附 key_hint 尾 4 位脱敏）。 */
export interface LlmConfigItem {
  id: number;
  name: string;
  base_url: string;
  model: string;
  has_key: boolean;
  key_hint: string | null;
  is_active: boolean;
  scope: LlmConfigScope;
  created_at: string;
  updated_at: string;
}

export interface LlmConfigTestResult {
  ok: boolean;
  latency_ms: number;
  error: string | null;
}

/** 读取指定 scope 的激活配置安全视图。 */
export async function getLlmConfig(scope: LlmConfigScope): Promise<LlmConfigOut> {
  return evoJson<LlmConfigOut>(`/api/config/llm?scope=${scope}`, { method: "GET" });
}

/** 读取指定 scope 的所有配置列表。 */
export async function listLlmConfigs(scope: LlmConfigScope): Promise<LlmConfigItem[]> {
  return evoJson<LlmConfigItem[]>(`/api/config/llm/list?scope=${scope}`, { method: "GET" });
}

/** 新建配置（按 scope 归属，该 scope 首条自动激活）。 */
export async function createLlmConfig(
  scope: LlmConfigScope,
  payload: {
    name: string;
    api_key: string;
    base_url: string;
    model: string;
  },
): Promise<LlmConfigItem> {
  return evoJson<LlmConfigItem>(`/api/config/llm?scope=${scope}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 更新配置。api_key 传空串=不改 key。按 id 操作，scope 由 id 隐含。 */
export async function updateLlmConfig(
  id: number,
  payload: {
    name?: string;
    api_key?: string;
    base_url?: string;
    model?: string;
  },
): Promise<LlmConfigItem> {
  return evoJson<LlmConfigItem>(`/api/config/llm/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 删除配置。删激活项会自动激活同 scope 剩余中 id 最小的一条。 */
export async function deleteLlmConfig(id: number): Promise<{ ok: boolean }> {
  return evoJson<{ ok: boolean }>(`/api/config/llm/${id}`, {
    method: "DELETE",
  });
}

/** 设为激活（scope 内唯一，scope 由 id 隐含）。 */
export async function activateLlmConfig(id: number): Promise<LlmConfigItem> {
  return evoJson<LlmConfigItem>(`/api/config/llm/${id}/activate`, {
    method: "POST",
  });
}

/**
 * 测试连通性。两条路径二选一：
 * - 测已存配置：传 { id }，后端读库解密。
 * - 测草稿：传 { api_key, base_url, model }。
 * 同时传时 id 优先。测试逻辑与 scope 正交（T3），故不带 scope 参数。
 */
export async function testLlmConfig(payload: {
  id?: number;
  api_key?: string;
  base_url?: string;
  model?: string;
}): Promise<LlmConfigTestResult> {
  return evoJson<LlmConfigTestResult>("/api/config/llm/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ════════════════════════════════════════════════════════════
//  监测（stats + active-runs + traces）
//  类型统一从 @/lib/types 引用（trace 移植后对齐）
// ════════════════════════════════════════════════════════════

export async function getStatsOverview(): Promise<StatsOverview> {
  return evoJson<StatsOverview>("/api/stats/overview", { method: "GET" });
}

export async function getStatsSkills(top = 20): Promise<SkillStat[]> {
  return evoJson<SkillStat[]>(`/api/stats/skills?top=${top}`, { method: "GET" });
}

export async function getStatsTimeline(hours = 168): Promise<TimelinePoint[]> {
  return evoJson<TimelinePoint[]>(`/api/stats/timeline?hours=${hours}`, { method: "GET" });
}

export async function getStatsFailures(top = 10): Promise<FailurePattern[]> {
  return evoJson<FailurePattern[]>(`/api/stats/failures?top=${top}`, { method: "GET" });
}

export async function getActiveRuns(): Promise<ActiveRun[]> {
  return evoJson<ActiveRun[]>("/api/active-runs", { method: "GET" });
}

// ── trace 列表 + 详情 ──

export async function getTraces(params?: {
  status?: string;
  run_purpose?: string;
  owner?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}): Promise<TraceListResponse> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.run_purpose) qs.set("run_purpose", params.run_purpose);
  if (params?.owner) qs.set("owner", params.owner);
  if (params?.since) qs.set("since", params.since);
  if (params?.until) qs.set("until", params.until);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.offset) qs.set("offset", String(params.offset));
  const q = qs.toString();
  return evoJson<TraceListResponse>(`/api/traces${q ? "?" + q : ""}`, { method: "GET" });
}

/** 用户缓存列表（trace 历史页用户筛选下拉用） */
export async function getUserCache(): Promise<UserCacheItem[]> {
  return evoJson<UserCacheItem[]>("/api/users/cache", { method: "GET" });
}

export async function getTraceDetail(traceId: string): Promise<TraceDetailLite> {
  return evoJson<TraceDetailLite>(`/api/traces/${traceId}`, { method: "GET" });
}

/**
 * 按 event_id 批量拉取原始事件（抽屉懒加载，Phase 2）。
 * 前端从 node.raw_event_ids 拿到事件 id 列表，调本接口拉取。
 */
export async function getTraceEvents(
  traceId: string,
  eventIds: string[],
): Promise<TraceLogEvent[]> {
  const ids = eventIds.join(",");
  return evoJson<TraceLogEvent[]>(`/api/traces/${traceId}/events?event_ids=${encodeURIComponent(ids)}`, {
    method: "GET",
  });
}

/**
 * 按 anchor_id 拉取单个 context segment（抽屉懒加载，Phase 2）。
 */
export async function getTraceContext(
  traceId: string,
  anchorId: string,
): Promise<TraceContextSegment> {
  return evoJson<TraceContextSegment>(
    `/api/traces/${traceId}/context?anchor_id=${encodeURIComponent(anchorId)}`,
    { method: "GET" },
  );
}

// ════════════════════════════════════════════════════════════
//  评估（eval-agent）
// ════════════════════════════════════════════════════════════

export interface EvalSession {
  eval_id: string;
  trace_id: string;
  status: string; // running | done | failed
  scores_json: string | null;
  findings_json: string | null;
  report_md: string | null;
  created_at: string;
  updated_at: string;
  scores: Record<string, any> | null;
  findings: any[] | null;
}

export async function startEval(traceId: string): Promise<{ eval_id: string; trace_id: string; status: string }> {
  return evoJson(`/api/eval-agent/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trace_id: traceId }),
  });
}

export async function stopEval(evalId: string): Promise<{ status: string; eval_id: string }> {
  return evoJson(`/api/eval-agent/sessions/${evalId}/stop`, { method: "POST" });
}

export async function getEvalSessions(limit = 50): Promise<{ sessions: EvalSession[]; total: number }> {
  return evoJson(`/api/eval-agent/sessions?limit=${limit}`, { method: "GET" });
}

export async function getEvalSession(evalId: string): Promise<EvalSession> {
  return evoJson<EvalSession>(`/api/eval-agent/sessions/${evalId}`, { method: "GET" });
}

export async function getEvaluatedTraces(limit = 100): Promise<{ traces: EvalSession[]; total: number }> {
  return evoJson(`/api/eval-agent/evaluated-traces?limit=${limit}`, { method: "GET" });
}

// ════════════════════════════════════════════════════════════
//  进化（evolve）
// ════════════════════════════════════════════════════════════

export interface EvolveSession {
  id: number;
  session_id: string;
  case_id: string;
  status: string; // running|done|failed|pending_review|published|discarded
  phase: string | null;
  baseline_trace: string | null;
  candidate_trace: string | null;
  baseline_score: number | null;
  candidate_score: number | null;
  report_json: string | null;
  created_at: string;
  updated_at: string | null;
  eval_ref: string | null;
  report?: any;
  // 审查视图内联字段（get_session 详情接口返回，列表接口不含）
  design_doc?: DesignDoc | null;
  change_log?: ChangeLog | null;
  eval_snapshot?: EvalSnapshot | null;
}

// ── 审查视图数据类型（D1：get_session 内联）──────────────────────

export interface DesignChange {
  target: string;
  change_desc: string;
  reason: string;
  evidence_ref?: string[]; // 引用 EvalFinding.id（f01/f02…）
  expected_up?: string;
  expected_down?: string;
  edit?: any;
}

export interface DesignDoc {
  meta: {
    designed_at: string;
    changes_count: number;
    changes: DesignChange[];
  };
  body: string;
}

export interface AppliedChange {
  target: string;
  action: string;
  result: "ok" | "failed";
  detail: string;
  design_ref?: number; // 对应 DesignChange 的序号（1-based）
}

export interface ChangeLog {
  meta: {
    executed_at: string;
    applied_count: number;
    applied: AppliedChange[];
    validation: { passed: boolean; config_valid?: boolean; import_ok?: boolean; errors?: string[] };
  };
  body: string;
}

export interface EvalFinding {
  id?: string;
  dimension: string;
  severity: string;
  evidence_type: string;
  finding: string;
  evidence: string;
}

export interface EvalSnapshot {
  eval_id?: string;
  trace_id?: string;
  findings: EvalFinding[] | null;
  scores: Record<string, any> | null;
}

export async function startEvolve(traceId: string): Promise<{ session_id: string; trace_id: string; eval_id: string; status: string }> {
  return evoJson(`/api/evolve/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trace_id: traceId }),
  });
}

export async function stopEvolve(sessionId: string): Promise<{ status: string; session_id: string }> {
  return evoJson(`/api/evolve/sessions/${sessionId}/stop`, { method: "POST" });
}

export async function getEvolveSessions(limit = 50): Promise<{ sessions: EvolveSession[]; total: number }> {
  return evoJson(`/api/evolve/sessions?limit=${limit}`, { method: "GET" });
}

export async function getEvolveSession(sessionId: string): Promise<EvolveSession> {
  return evoJson<EvolveSession>(`/api/evolve/sessions/${sessionId}`, { method: "GET" });
}

export async function publishEvolve(sessionId: string): Promise<{ status: string; snapshot_version: number; source_commit: string }> {
  return evoJson(`/api/evolve/sessions/${sessionId}/publish`, { method: "POST" });
}

export async function discardEvolve(sessionId: string): Promise<{ status: string; reset_to: string }> {
  return evoJson(`/api/evolve/sessions/${sessionId}/discard`, { method: "POST" });
}

// ── 对话式共创工作台（Phase 4，决策 T2/T10）──────────────────────

/** 进化对话消息（evolve_messages 表，决策 T6） */
export interface EvolveMessage {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string; // markdown
  tool_events?: any[] | null; // assistant 消息触发的工具调用列表
  related_points?: string[] | null; // 涉及的进化点 id（双向高亮联动）
  seq: number;
  created_at: string;
}

/** 进化点 option（备选方案，决策 T） */
export interface EvolvePointOption {
  description: string;
  pros: string[];
  cons: string[];
  expected_impact: string;
}

/** 进化点（evolve_points 表，决策 T7/B/T） */
export interface EvolvePoint {
  id: string;
  session_id: string;
  seq: number;
  target: string;
  problem: string;
  options: EvolvePointOption[];
  recommendation?: string | null;
  note?: string | null;
  status: "proposed" | "accepted" | "rejected";
  chosen_option?: number | null; // 0-based，accepted 时
  user_note?: string | null;
  accepted_at?: string | null;
  design_ref?: number | null;
  created_at: string;
}

/** 架构蓝图 API 返回（决策 Q） */
export interface EvolveSystemPrompt {
  blueprint: string; // markdown
  version: string;
}

export async function getEvolveSystemPrompt(): Promise<EvolveSystemPrompt> {
  return evoJson<EvolveSystemPrompt>(`/api/evolve/system-prompt`, { method: "GET" });
}

/** 对话式启动进化（决策 T2，inspect round + 转 conversing） */
export async function startEvolveConverse(
  traceId: string,
): Promise<{ session_id: string; trace_id: string; eval_id: string; status: string }> {
  return evoJson(`/api/evolve/start-converse`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trace_id: traceId }),
  });
}

export async function getEvolveMessages(
  sessionId: string,
  afterSeq?: number,
): Promise<{ messages: EvolveMessage[] }> {
  const qs = afterSeq !== undefined ? `?after_seq=${afterSeq}` : "";
  return evoJson(`/api/evolve/sessions/${sessionId}/messages${qs}`, { method: "GET" });
}

export async function sendEvolveMessage(
  sessionId: string,
  content: string,
): Promise<{ message_id: string; seq: number; session_id: string; status: string }> {
  return evoJson(`/api/evolve/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
}

export async function getEvolvePoints(
  sessionId: string,
): Promise<{ points: EvolvePoint[]; accepted_count: number }> {
  return evoJson(`/api/evolve/sessions/${sessionId}/points`, { method: "GET" });
}

export async function finalizeEvolve(
  sessionId: string,
): Promise<{ session_id: string; status: string; accepted_count: number }> {
  return evoJson(`/api/evolve/sessions/${sessionId}/finalize`, { method: "POST" });
}

// ════════════════════════════════════════════════════════════
//  单次测试（tests）
// ════════════════════════════════════════════════════════════

export interface ManualTest {
  test_id: string;
  case_id: string;
  version_type: string; // working | snapshot
  version_id: number | null;
  trace_id: string | null;
  task_id: string | null;
  status: string; // pending|running|done|failed|cancelled
  error: string | null;
  retry_of: string | null;
  origin_layer: string | null;
  created_at: string;
}

export interface TestAgentOption {
  type: string; // working | snapshot
  label: string;
  version?: number;
  source_commit?: string;
  change_summary?: string;
}

export async function getTests(params?: {
  status?: string;
  page?: number;
  page_size?: number;
}): Promise<{ tests: ManualTest[]; total: number; page: number; page_size: number }> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  qs.set("page", String(params?.page ?? 1));
  qs.set("page_size", String(params?.page_size ?? 20));
  return evoJson(`/api/tests?${qs.toString()}`, { method: "GET" });
}

export async function startTest(payload: {
  case_id: string;
  version_type: string;
  version_id?: number | null;
}): Promise<{ test_id: string; status: string }> {
  return evoJson(`/api/tests`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function getTestAgents(): Promise<{ agents: TestAgentOption[] }> {
  return evoJson(`/api/tests/agents`, { method: "GET" });
}

export async function retryTest(testId: string): Promise<{ test_id: string; status: string }> {
  return evoJson(`/api/tests/${testId}/retry`, { method: "POST" });
}

export async function stopTest(testId: string): Promise<{ status: string; test_id: string }> {
  return evoJson(`/api/tests/${testId}/stop`, { method: "POST" });
}

export async function deleteTest(testId: string): Promise<{ status: string; deleted: string }> {
  const resp = await evoFetch(`/api/tests/${testId}`, { method: "DELETE" });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

// ════════════════════════════════════════════════════════════
//  Harness 要素（snapshots/harness-elements）
// ════════════════════════════════════════════════════════════

export interface Snapshot {
  version: number;
  parent_version: number | null;
  source_commit: string | null;
  change_summary: string | null;
  status: string; // production | retired
  created_at: string;
}

export interface HarnessElementView {
  name: string;
  kind: string; // meta | subagent
  prompt: { body: string };
  skills: { path: string; name: string; description: string | null; content: string | null; load_error: string | null }[];
  middlewares: {
    hook: string | null;
    group: string | null;
    class_name: string | null;
    params: Record<string, any>;
    source_path: string | null;
    description: string | null;
  }[];
}

/** Tool 作用域——诚实反映 harness 里 tool 的真实归属（非全局即 agent） */
export type ToolScope =
  | { kind: "global" }                  // 全局注入（如进 MemoryRetriever 单例）
  | { kind: "middleware"; via: string } // 经某 middleware 暴露
  | { kind: "agent"; agent: string }    // 仅某 agent 间接受益
  | { kind: "memory" }                  // 记忆系统策略要素
  | { kind: "unknown" };                // TOOL_SCOPE_MAP 未登记，前端显示提醒

/** tools/ 目录下一个可进化 tool 文件 */
export interface ToolInfo {
  path: string;                  // 如 "tools/goal.py"
  name: string;                  // 文件名去后缀，如 "goal"
  description: string | null;    // 模块 docstring 首句
  scope: ToolScope;              // 作用域标注
  load_error: string | null;     // 解析失败时填
}

export interface HarnessElementsView {
  source_commit: string | null;
  has_source: boolean;
  agents: HarnessElementView[];
  tools: ToolInfo[]; // 顶层平级——tools/ 是全局平铺的，不属于任何 agent
  subagent_relations: { from: string; to: string; role: string }[];
}

export async function getSnapshots(): Promise<Snapshot[]> {
  return evoJson<Snapshot[]>(`/api/snapshots`, { method: "GET" });
}

export async function getProductionSnapshot(): Promise<Snapshot> {
  return evoJson<Snapshot>(`/api/snapshots/production`, { method: "GET" });
}

export async function getHarnessElements(version: number): Promise<HarnessElementsView> {
  return evoJson<HarnessElementsView>(`/api/snapshots/${version}/harness-elements`, { method: "GET" });
}

// ── 记忆子系统要素（NWM 6 要素，独立于按 agent 分组的 HarnessElementsView） ──

/** 记忆协同链中的角色（与后端 versioning.constants.MEMORY_ROLE_ORDER 对齐） */
export type MemoryFileRole = "extract" | "store" | "retrieve" | "recall";

/** 记忆要素物理类型 */
export type MemoryElementType = "prompt" | "middleware" | "tool";

/** 单个记忆要素：NWM 协同链的一个环节 */
export interface MemoryElementView {
  name: string;
  path: string; // 相对 harness 包根，如 tools/query_builder.py
  type: MemoryElementType;
  file_role: MemoryFileRole;
  description: string;
  tags: string[]; // 恒为 ["memory"]
}

/** GET /api/snapshots/{version}/harness-elements/memory 响应 */
export interface MemoryElementsView {
  version: number;
  has_source: boolean;
  elements: MemoryElementView[]; // 已按 抽取→存储→检索→回填 排序
}

export async function getMemoryElements(version: number): Promise<MemoryElementsView> {
  return evoJson<MemoryElementsView>(`/api/snapshots/${version}/harness-elements/memory`, { method: "GET" });
}

// ── 版本源码全文（懒加载，供 Memory Tab 点击展开看要素源码） ──

/** GET /api/snapshots/{version}/source?path= 响应：单文件源码全文 */
export interface SnapshotSource {
  path: string;
  content: string;
}

/**
 * 读指定版本指定文件的源码全文。
 * path 相对 harness 包根（如 tools/narrative_schema.py）。
 * 后端按 version→commit 映射后 git show，老版本文件不存在返回 404。
 */
export async function getSnapshotSource(version: number, path: string): Promise<SnapshotSource> {
  return evoJson<SnapshotSource>(
    `/api/snapshots/${version}/source?path=${encodeURIComponent(path)}`,
    { method: "GET" },
  );
}

// ── 版本详情（含升级 diff + 改动意图，来自 GET /api/versions/{version}） ──

/** 行级 diff 的一个 hunk：equal/insert/delete 三种，无 replace（后端已拆为 del+ins） */
export interface Hunk {
  type: "equal" | "insert" | "delete";
  lines: string[];
}

/** prompt diff：hunks 序列 + 增删行数摘要 */
export interface PromptDiff {
  hunks: Hunk[];
  summary: { added: number; removed: number };
}

/** skills diff：路径集合差 */
export interface SkillsDiff {
  added: string[];
  removed: string[];
  unchanged_count: number;
}

/** middleware（processor）的单条变更 */
export interface ProcessorChange {
  key: { hook: string; group: string };
  change_type: "added" | "removed" | "modified";
  class_change: { old: string | null; new: string | null };
  params_change: { old: Record<string, any> | null; new: Record<string, any> | null };
}

/** 单个 agent 的三要素 diff。whole_agent 仅整 agent 增删时存在，常规修改缺失 */
export interface AgentDiff {
  prompt?: PromptDiff | null;
  skills?: SkillsDiff | null;
  processors?: ProcessorChange[];
  whole_agent?: "added" | "removed";
}

/** design_doc 改动意图的单条。五字段恒 string，缺失填 "" */
export interface IntentItem {
  target: string;
  change_desc: string;
  reason: string;
  expected_up: string;
  expected_down: string;
}

/** version_changes 表的投影：按 agent 聚合的客观 diff + 版本级主观意图 */
export interface VersionChanges {
  agents: { agent: string; diff: AgentDiff }[];
  intent: IntentItem[] | null;
}

/** GET /api/versions/{version} 响应（仅 harness 页需要的字段） */
export interface VersionDetail {
  version: number;
  parent_version: number | null;
  is_bootstrap: boolean;
  change_summary: string | null;
  changes: VersionChanges;
}

export async function getVersionDetail(version: number): Promise<VersionDetail> {
  return evoJson<VersionDetail>(`/api/versions/${version}`, { method: "GET" });
}

// ── 版本谱系列表（GET /api/versions）──
// 注意：后端端点仍含旧 adapt 残留字段（reward/source_round/critic_verdict），
// 此类型只取 registry 谱系字段，忽略 adapt 残留。完整 diff 待 version_changes 写入层修复后另做。

/** 版本谱系单条（只用 registry 谱系字段） */
export interface VersionListItem {
  version: number;
  parent_version: number | null;
  status: string; // production | retired
  change_summary: string | null;
  created_at: string;
  source_session: string | null;
}

/** GET /api/versions 响应 */
export interface VersionsListResponse {
  items: VersionListItem[];
  total: number;
  production_version: number;
  limit: number;
  offset: number;
}

export async function getVersions(): Promise<VersionsListResponse> {
  return evoJson<VersionsListResponse>(`/api/versions?limit=200`, { method: "GET" });
}

// ════════════════════════════════════════════════════════════
//  数据集（dataset）— golden/growing 评估集
// ════════════════════════════════════════════════════════════

export interface DatasetCase {
  case_id: string;
  title: string;
  layer: "golden" | "growing";
  source_trace_id: string | null;
  demand_revision: string | null;
  promoted_at: string | null;
  created_by: string;
  has_reference: boolean;
}

export interface GoldenRevision {
  revision: string;
  locked: boolean;
  intact: boolean;
  case_count: number;
  cases: string[];
}

/** 列出数据集 case（按 layer 过滤，不传=全部）。 */
export async function getDatasetCases(
  layer?: "golden" | "growing",
): Promise<{ cases: DatasetCase[]; total: number }> {
  const q = layer ? `?layer=${layer}` : "";
  return evoJson(`/api/dataset/cases${q}`, { method: "GET" });
}

/** 单 case 内容（demand.md + reference.md 全文 + 元数据）。 */
export async function getCaseContent(
  caseId: string,
  layer?: "golden" | "growing",
): Promise<{
  case_id: string;
  title: string;
  layer: string;
  demand_md: string;
  reference_md: string | null;
  source_trace_id: string | null;
  demand_revision: string | null;
  promoted_at: string | null;
  created_by: string;
  status: string;
}> {
  const q = layer ? `?layer=${layer}` : "";
  return evoJson(`/api/dataset/cases/${caseId}${q}`, { method: "GET" });
}

/** 当前 golden 集锁定的 revision + 完整性状态。 */
export async function getGoldenRevision(): Promise<GoldenRevision> {
  return evoJson<GoldenRevision>("/api/dataset/golden-revision", { method: "GET" });
}

// ════════════════════════════════════════════════════════════
//  标注队列（promote）— 生产 trace → growing 的标注闸门
// ════════════════════════════════════════════════════════════

/** judge 打分摘要（存 promote_tasks.judge_scores，结构见 judge._extract_scores_summary）。 */
export interface JudgeScores {
  content_overall: number;
  content_scores: Record<string, any>;
  subagent_scores: Record<string, number>;
  is_badcase: boolean;
  flagged_count: number;
  [k: string]: any;
}

export interface PromoteTask {
  task_id: string;
  trace_id: string;
  owner_user_id: string | null;
  status: string; // pending|judging|needs_confirm|rejected|promoted
  judge_verdict: string | null; // auto_promote|needs_human|auto_reject
  judge_scores: JudgeScores | null;
  created_at: string;
  decided_at: string | null;
}

export interface PromoteTaskDetail extends PromoteTask {
  trace?: {
    trace_id: string;
    status: string;
    owner_user_id: string;
    started_at: string | null;
    ended_at: string | null;
    duration_ms: number | null;
    session_name: string | null;
  };
  deliveries?: Record<string, any>;
  annotator?: string | null;
  decision?: string | null;
  target_case_id?: string | null;
}

/** 标注队列列表（默认只看活跃态 pending/judging/needs_confirm）。 */
export async function getPromoteTasks(params?: {
  status?: string;
  page?: number;
  page_size?: number;
}): Promise<{ tasks: PromoteTask[]; total: number; page: number; page_size: number }> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  qs.set("page", String(params?.page ?? 1));
  qs.set("page_size", String(params?.page_size ?? 50));
  return evoJson(`/api/promote/tasks?${qs.toString()}`, { method: "GET" });
}

/** 标注详情（trace 摘要 + judge 分数 + 交付物概要）。 */
export async function getPromoteTaskDetail(taskId: string): Promise<PromoteTaskDetail> {
  return evoJson<PromoteTaskDetail>(`/api/promote/tasks/${taskId}`, { method: "GET" });
}

/** 提交标注决策。accept 必须二选一：target_case_id（归入）或 new_case_title（新建）。 */
export async function decidePromoteTask(
  taskId: string,
  payload: {
    decision: "accept" | "reject";
    annotator?: string;
    target_case_id?: string;
    new_case_title?: string;
    demand_md?: string;
    reference_output?: string;
  },
): Promise<{ task_id: string; status: string; case_id?: string; has_reference?: boolean }> {
  return evoJson(`/api/promote/tasks/${taskId}/decide`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ════════════════════════════════════════════════════════════
//  管理后台（走 executor /api/admin/*，需 super_admin）
//  与 evolution 域不同——这些接口在 executor 上，用 apiFetch（无 /evolution-api 前缀）。
// ════════════════════════════════════════════════════════════

export interface AdminUser {
  user_id: string;
  username: string;
  is_admin: boolean;
  is_super_admin: boolean;
  disabled: boolean;
  has_api_key: boolean;
  credits_balance: number;
  workspace_count: number;
  created_at: string;
}

export interface InviteCode {
  code: string;
  created_at: string;
  is_admin_code: boolean;
  granted_credits: number;
  used: boolean;
  used_by: string | null;
  used_at: string | null;
  revoked_at: string | null;
}

export interface CreditTransaction {
  tx_id: string;
  user_id: string;
  type: string;
  amount: number;
  balance_after: number;
  ref_thread_id: string | null;
  ref_hold_id: string | null;
  note: string | null;
  created_by: string | null;
  created_at: string;
}

export interface CreditConfigItem {
  value: string;
  description: string;
  updated_at: string;
}

// ── 用户管理 ──────────────────────────────────────────────────

export async function fetchUsers(): Promise<AdminUser[]> {
  return apiJson<AdminUser[]>("/api/admin/users", { method: "GET" });
}

export async function updateUser(
  userId: string,
  payload: { disabled?: boolean },
): Promise<Record<string, unknown>> {
  return apiJson(`/api/admin/users/${userId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function resetPassword(
  userId: string,
): Promise<{ status: string; temp_password: string }> {
  return apiJson(`/api/admin/users/${userId}/reset-password`, { method: "POST" });
}

export async function adjustCredits(
  userId: string,
  amount: number,
  note: string,
): Promise<{ status: string; balance: number }> {
  return apiJson(`/api/admin/users/${userId}/credits`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ amount, note }),
  });
}

// ── 邀请码管理 ────────────────────────────────────────────────

export async function fetchInviteCodes(): Promise<InviteCode[]> {
  return apiJson<InviteCode[]>("/api/admin/invite-codes", { method: "GET" });
}

export async function createInviteCodes(
  count: number,
  grantedCredits: number,
): Promise<string[]> {
  return apiJson<string[]>("/api/admin/invite-codes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ count, granted_credits: grantedCredits }),
  });
}

export async function revokeInviteCode(code: string): Promise<Record<string, unknown>> {
  return apiJson(`/api/admin/invite-codes/${code}`, { method: "DELETE" });
}

// ── 积分流水 ──────────────────────────────────────────────────

export async function fetchAllTransactions(limit = 100): Promise<CreditTransaction[]> {
  return apiJson<CreditTransaction[]>(`/api/admin/credits/transactions?limit=${limit}`, {
    method: "GET",
  });
}

// ── 积分配置（暗调旋钮）──────────────────────────────────────

export async function fetchCreditsConfig(): Promise<Record<string, CreditConfigItem>> {
  return apiJson<Record<string, CreditConfigItem>>("/api/admin/credits/config", {
    method: "GET",
  });
}

export async function updateCreditsConfig(
  key: string,
  value: string,
): Promise<Record<string, unknown>> {
  return apiJson(`/api/admin/credits/config/${key}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
}
