import { invoke } from "@tauri-apps/api/core";

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
 */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null) {
  onUnauthorized = handler;
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

  const relay = await invoke<HttpRelayResponse>("http_request", {
    request: {
      path: input,
      method: init.method || "GET",
      headers: Object.keys(headers).length ? headers : null,
      body: bodyValue ?? null,
      stream: false,
    },
  });

  // 401 → 触发跳登录
  if (relay.status === 401 && onUnauthorized) {
    onUnauthorized();
  }

  return new RelayResponse(relay);
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
// ════════════════════════════════════════════════════════════

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
  created_at: string;
  updated_at: string;
}

export interface LlmConfigTestResult {
  ok: boolean;
  latency_ms: number;
  error: string | null;
}

/** 读取激活配置安全视图（旧契约，首页 status 用）。 */
export async function getLlmConfig(): Promise<LlmConfigOut> {
  return evoJson<LlmConfigOut>("/api/config/llm", { method: "GET" });
}

/** 读取所有配置列表。 */
export async function listLlmConfigs(): Promise<LlmConfigItem[]> {
  return evoJson<LlmConfigItem[]>("/api/config/llm/list", { method: "GET" });
}

/** 新建配置。首条自动激活。 */
export async function createLlmConfig(payload: {
  name: string;
  api_key: string;
  base_url: string;
  model: string;
}): Promise<LlmConfigItem> {
  return evoJson<LlmConfigItem>("/api/config/llm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 更新配置。api_key 传空串=不改 key。 */
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

/** 删除配置。删激活项会自动激活剩余中 id 最小的一条。 */
export async function deleteLlmConfig(id: number): Promise<{ ok: boolean }> {
  return evoJson<{ ok: boolean }>(`/api/config/llm/${id}`, {
    method: "DELETE",
  });
}

/** 设为激活（全局唯一）。 */
export async function activateLlmConfig(id: number): Promise<LlmConfigItem> {
  return evoJson<LlmConfigItem>(`/api/config/llm/${id}/activate`, {
    method: "POST",
  });
}

/**
 * 测试连通性。两条路径二选一：
 * - 测已存配置：传 { id }，后端读库解密。
 * - 测草稿：传 { api_key, base_url, model }。
 * 同时传时 id 优先。
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
// ════════════════════════════════════════════════════════════

export interface StatsOverview {
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
}

export interface SkillStat {
  agent_name: string;
  call_count: number;
  node_count: number;
  avg_duration_ms: number | null;
  fail_count: number;
  fail_rate: number;
}

export interface TimelinePoint {
  bucket: string;
  total: number;
  failed: number;
}

export interface FailurePattern {
  error_pattern: string;
  count: number;
  sample_trace_ids: string[];
}

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

export interface ActiveRun {
  trace_id: string;
  workspace_id: string;
  thread_id: string | null;
  endpoint: string | null;
  status: string;
  started_at: string | null;
  duration_ms: number | null;
  event_count: number;
  session_name: string | null;
  ingested: boolean;
}

export async function getActiveRuns(): Promise<ActiveRun[]> {
  return evoJson<ActiveRun[]>("/api/active-runs", { method: "GET" });
}

// ── trace 列表 + 详情 ──

export interface TraceListItem {
  trace_id: string;
  workspace_id: string;
  thread_id: string | null;
  session_name: string | null;
  endpoint: string | null;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  event_count: number;
  error: string | null;
  owner_user_id: string;
  run_purpose: string;
}

export async function getTraces(params?: {
  status?: string;
  run_purpose?: string;
  limit?: number;
  offset?: number;
}): Promise<TraceListItem[]> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.run_purpose) qs.set("run_purpose", params.run_purpose);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.offset) qs.set("offset", String(params.offset));
  const q = qs.toString();
  return evoJson<TraceListItem[]>(`/api/traces${q ? "?" + q : ""}`, { method: "GET" });
}

export interface TraceDetail {
  run: {
    trace_id: string;
    workspace_id: string;
    status: string;
    started_at: string | null;
    ended_at: string | null;
    duration_ms: number | null;
    event_count: number;
    error: string | null;
    [k: string]: any;
  };
  events: any[];
  nodes: any[];
  context: any[];
  todos: any[];
}

export async function getTraceDetail(traceId: string): Promise<TraceDetail> {
  return evoJson<TraceDetail>(`/api/traces/${traceId}`, { method: "GET" });
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
}

export async function startEvolve(traceId: string): Promise<{ session_id: string; trace_id: string; eval_id: string; status: string }> {
  return evoJson(`/api/evolve/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trace_id: traceId }),
  });
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
//  Agent 要素（snapshots/elements）
// ════════════════════════════════════════════════════════════

export interface Snapshot {
  version: number;
  parent_version: number | null;
  source_commit: string | null;
  change_summary: string | null;
  status: string; // production | retired
  created_at: string;
}

export interface AgentElementView {
  name: string;
  kind: string; // meta | subagent
  prompt: { body: string };
  skills: { path: string; name: string; content: string | null; load_error: string | null }[];
  middlewares: {
    hook: string | null;
    group: string | null;
    class_name: string | null;
    params: Record<string, any>;
    source_path: string | null;
  }[];
}

export interface ElementsView {
  source_commit: string | null;
  has_source: boolean;
  agents: AgentElementView[];
  subagent_relations: { from: string; to: string; role: string }[];
}

export async function getSnapshots(): Promise<Snapshot[]> {
  return evoJson<Snapshot[]>(`/api/snapshots`, { method: "GET" });
}

export async function getProductionSnapshot(): Promise<Snapshot> {
  return evoJson<Snapshot>(`/api/snapshots/production`, { method: "GET" });
}

export async function getElements(version: number): Promise<ElementsView> {
  return evoJson<ElementsView>(`/api/snapshots/${version}/elements`, { method: "GET" });
}
