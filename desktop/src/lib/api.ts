import { invoke } from "@tauri-apps/api/core";
import type { CharacterGenerateRequest, CharacterGenerateResponse, CheckpointState, InitResponse, Style, ThreadSummary, TraceDetail, TraceRunSummary, WorkspaceBootstrapResponse, WorkspaceCharacterContent, WorkspaceDetailOutlineContent, WorkspaceNovelContent, WorkspaceOutlineContent, WorkspaceStorylineContent, WorkspaceWorldviewContent, WorkspaceStorylineGraphContent, WorkspaceSummary } from "./types";

/**
 * 桌面端所有请求走 Rust 中继（设计文档 S4/S5）：
 * - API_BASE_URL 留空 → 业务函数里 `${API_BASE_URL}/api/xxx` 退化为纯 path `/api/xxx`。
 * - apiFetch 内部 invoke("http_request")，Rust 端拼 server_url + 处理 cookie/SSE。
 * - 原 frontend 的 60 处业务代码无需改动（path 拼接逻辑不变）。
 */
export const API_BASE_URL = "";

/// Rust http_request command 的返回结构（对应 src-tauri/src/http.rs HttpResponse）。
interface HttpRelayResponse {
  status: number;
  headers: Record<string, string>;
  body: string;
}

/**
 * 401 跳转回调：由路由层注入（api.ts 不直接耦合 react-router）。
 * 桌面端：登录失效 → 跳 /login。
 * 未注入时（如启动早期），401 静默不跳。
 */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null) {
  onUnauthorized = handler;
}

/**
 * 仿 Response 对象：让现有 parseJsonResponse（用 .ok/.status/.json()）无需改动。
 * 桌面端不返回真实 fetch Response，而是包装 Rust 中继的结果。
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

/**
 * 统一请求封装（替代原 fetch）：
 * - 走 invoke("http_request")，Rust reqwest 带自动 cookie jar。
 * - 401 触发 onUnauthorized 回调（路由层跳 /login）。
 *
 * input 是纯 path（如 "/api/auth/login"），Rust 端拼 server_url。
 * init 兼容原 RequestInit（method/headers/body），body 是 JSON 字符串时反序列化为对象传给 Rust。
 */
export async function apiFetch(input: string, init: RequestInit = {}): Promise<RelayResponse> {
  // 解析 body：原 frontend 传 JSON.stringify(...) 字符串，Rust 端要 serde_json::Value。
  let bodyValue: unknown = undefined;
  if (init.body) {
    const raw = typeof init.body === "string" ? init.body : String(init.body);
    try {
      bodyValue = JSON.parse(raw);
    } catch {
      bodyValue = raw;
    }
  }

  // headers：原 init.headers 可能是 Record 或 Headers，统一拍平成 Record。
  const headers: Record<string, string> = {};
  if (init.headers) {
    if (init.headers instanceof Headers) {
      init.headers.forEach((v, k) => { headers[k] = v; });
    } else if (Array.isArray(init.headers)) {
      for (const [k, v] of init.headers) { headers[k] = v; }
    } else {
      Object.assign(headers, init.headers);
    }
  }

  const relay = await invoke<HttpRelayResponse>("http_request", {
    request: {
      path: input,
      method: init.method ?? "GET",
      headers: Object.keys(headers).length ? headers : null,
      body: bodyValue ?? null,
      stream: false,
    },
  });

  const response = new RelayResponse(relay);

  // 401 → 触发跳转回调（避免在 /login 自身循环——回调内部判断）
  if (response.status === 401 && onUnauthorized) {
    onUnauthorized();
  }

  return response;
}

async function parseJsonResponse<T>(response: RelayResponse): Promise<T> {
  if (!response.ok) {
    throw new Error(`API returned ${response.status}`);
  }

  return (await response.json()) as T;
}

async function apiJson<T>(input: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(input, init);
  return parseJsonResponse<T>(response);
}

// ── 认证与账号（D4/D10/D11）───────────────────────────────

export type AuthMe = {
  user_id: string;
  username: string;
  is_admin: boolean;
  has_api_key: boolean;
};

export async function register(code: string, username: string, password: string) {
  return apiJson<AuthMe>(`${API_BASE_URL}/api/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, username, password }),
  });
}

export async function login(username: string, password: string) {
  return apiJson<AuthMe>(`${API_BASE_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
}

export async function logout() {
  return apiJson<{ ok: boolean }>(`${API_BASE_URL}/api/auth/logout`, { method: "POST" });
}

export async function fetchMe() {
  return apiJson<AuthMe>(`${API_BASE_URL}/api/auth/me`);
}

/** 非抛错版 fetchMe：用于路由守卫探测登录态。返回 null 表示未登录。 */
export async function fetchMeOrNull(): Promise<AuthMe | null> {
  try {
    const response = await apiFetch(`${API_BASE_URL}/api/auth/me`);
    if (!response.ok) return null;
    return (await response.json()) as AuthMe;
  } catch {
    return null;
  }
}

// ── 用户设置：API key（D9/D11）────────────────────────────

export async function setApiKey(apiKey: string, baseUrl: string) {
  return apiJson<{ has_api_key: boolean }>(`${API_BASE_URL}/api/me/api-key`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey, base_url: baseUrl }),
  });
}

export async function clearApiKey() {
  return apiJson<{ has_api_key: boolean }>(`${API_BASE_URL}/api/me/api-key`, {
    method: "DELETE",
  });
}

export type MyProfile = {
  username: string;
  has_api_key: boolean;
  base_url: string | null;
  active_model: string | null;
  workspace_count: number;
  workspace_quota: number;
};

export async function fetchMyProfile() {
  return apiJson<MyProfile>(`${API_BASE_URL}/api/me`);
}

// ── 积分余额（D11 写作端展示）──────────────────────────────

export async function fetchMyCredits(): Promise<{ balance: number }> {
  return apiJson<{ balance: number }>(`${API_BASE_URL}/api/me/credits`);
}

// ── Provider 配置历史（多条，可切换）──────────────────────

export type ProviderConfig = {
  config_id: string;
  name: string;
  base_url: string | null;
  model: string;
  is_active: boolean;
  created_at: string;
  last_used_at: string | null;
};

export type ProviderConfigInput = {
  name: string;
  api_key: string;
  base_url: string | null;
  model: string;
  activate?: boolean;
};

export async function listProviderConfigs() {
  return apiJson<ProviderConfig[]>(`${API_BASE_URL}/api/me/provider-configs`);
}

export async function createProviderConfig(payload: ProviderConfigInput) {
  return apiJson<ProviderConfig>(`${API_BASE_URL}/api/me/provider-configs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function updateProviderConfig(
  configId: string,
  payload: Partial<Omit<ProviderConfigInput, "activate">>,
) {
  return apiJson<ProviderConfig>(`${API_BASE_URL}/api/me/provider-configs/${configId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function activateProviderConfig(configId: string) {
  return apiJson<{ status: string; active: string }>(
    `${API_BASE_URL}/api/me/provider-configs/${configId}/activate`,
    { method: "POST" },
  );
}

export async function deleteProviderConfig(configId: string) {
  return apiJson<{ status: string; deleted: string }>(
    `${API_BASE_URL}/api/me/provider-configs/${configId}`,
    { method: "DELETE" },
  );
}

// ── 管理后台（D6/D12/D14）──────────────────────────────────

export type InviteCodeSummary = {
  code: string;
  created_at: string;
  is_admin_code: boolean;
  used: boolean;
  used_by: string | null;
  used_at: string | null;
  revoked_at: string | null;
};

export type AdminUserSummary = {
  user_id: string;
  username: string;
  is_admin: boolean;
  disabled: boolean;
  has_api_key: boolean;
  workspace_count: number;
  created_at: string;
};

export type AdminWorkspaceSummary = {
  workspace_id: string;
  title: string;
  domain: string;
  created_at: string;
  updated_at: string;
};

export async function adminListInviteCodes() {
  return apiJson<InviteCodeSummary[]>(`${API_BASE_URL}/api/admin/invite-codes`);
}

export async function adminCreateInviteCodes(count: number) {
  return apiJson<string[]>(`${API_BASE_URL}/api/admin/invite-codes`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ count }),
  });
}

export async function adminRevokeInviteCode(code: string) {
  return apiJson<{ status: string; revoked: string }>(
    `${API_BASE_URL}/api/admin/invite-codes/${encodeURIComponent(code)}`,
    { method: "DELETE" },
  );
}

export async function adminListUsers() {
  return apiJson<AdminUserSummary[]>(`${API_BASE_URL}/api/admin/users`);
}

export async function adminUpdateUser(
  userId: string,
  payload: { disabled?: boolean; reset_password?: string },
) {
  return apiJson<Record<string, unknown>>(`${API_BASE_URL}/api/admin/users/${userId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function adminResetUserPassword(userId: string) {
  return apiJson<{ status: string; temp_password: string }>(
    `${API_BASE_URL}/api/admin/users/${userId}/reset-password`,
    { method: "POST" },
  );
}

export async function adminListUserWorkspaces(userId: string) {
  return apiJson<AdminWorkspaceSummary[]>(
    `${API_BASE_URL}/api/admin/users/${userId}/workspaces`,
  );
}

export async function adminReadUserWorkspaceOutline(userId: string, workspaceId: string) {
  return apiJson<{ workspace_id: string; title: string; markdown: string }>(
    `${API_BASE_URL}/api/admin/users/${userId}/workspaces/${workspaceId}/outline`,
  );
}

// ── 业务接口（全部经 apiFetch 携带 cookie）─────────────────

export async function fetchWorkspaces() {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces`);
  return parseJsonResponse<WorkspaceSummary[]>(response);
}

export async function fetchInit() {
  const response = await apiFetch(`${API_BASE_URL}/api/init`);
  return parseJsonResponse<InitResponse>(response);
}

export async function fetchWorkspaceBootstrap(workspaceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/bootstrap`);
  return parseJsonResponse<WorkspaceBootstrapResponse>(response);
}

export async function createWorkspace(title: string, domain: string = "writing") {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, domain }),
  });

  return parseJsonResponse<WorkspaceSummary>(response);
}

// ── 文生图（Phase 3/4）────────────────────────────────────

/** 图片 URL（DD8b：按 image_id 取图，后端鉴权）。 */
export function imageUrl(imageId: string): string {
  return `${API_BASE_URL}/api/images/${imageId}`;
}

export type ImageVersion = {
  version_id: string;
  direction: string;
  prompt: string;
  images: { image_id: string; url: string }[];
  agent_analysis: string;
};

export type ImageReviewInterrupt = {
  kind: "image_review";
  round: number;
  versions: ImageVersion[];
};

export type ImageReviewResume = {
  kind: "image_review";
  round: number;
  ratings: { version_id: string; score: number; note: string }[];
  overall_direction: string;
  action: "continue" | "stop";
};

// ── Skill 管理（D18）──────────────────────────────────────

export type SkillSummary = {
  skill_id: string;
  name: string;
  scene_tag: string | null;
  description: string;
  revision_count: number;
  created_at: string;
  updated_at: string;
};

export type SkillDetail = SkillSummary & { content: string };

export async function listSkills() {
  return apiJson<SkillSummary[]>(`${API_BASE_URL}/api/skills`);
}

export async function readSkill(skillId: string) {
  return apiJson<SkillDetail>(`${API_BASE_URL}/api/skills/${skillId}`);
}

export async function updateSkill(
  skillId: string,
  data: { name?: string; scene_tag?: string; description?: string },
) {
  return apiJson<SkillSummary>(`${API_BASE_URL}/api/skills/${skillId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function deleteSkill(skillId: string) {
  return apiJson<{ status: string }>(`${API_BASE_URL}/api/skills/${skillId}`, {
    method: "DELETE",
  });
}

export async function mergeSkills(
  skillId1: string,
  skillId2: string,
  newName: string,
  newContent: string,
  newSceneTag: string = "",
) {
  return apiJson<SkillSummary>(`${API_BASE_URL}/api/skills/merge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      skill_id_1: skillId1,
      skill_id_2: skillId2,
      new_name: newName,
      new_content: newContent,
      new_scene_tag: newSceneTag,
    }),
  });
}

export async function deleteWorkspace(workspaceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces/${workspaceId}`, {
    method: "DELETE",
  });

  return parseJsonResponse<{ status: string; deleted: string; deleted_threads: string[] }>(response);
}

export async function fetchThreads(workspaceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/threads?workspace_id=${workspaceId}`);
  return parseJsonResponse<ThreadSummary[]>(response);
}

export async function createThread(workspaceId: string, sessionName?: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/threads`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: workspaceId, session_name: sessionName }),
  });

  return parseJsonResponse<ThreadSummary>(response);
}

export async function updateThread(threadId: string, sessionName: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/threads/${threadId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_name: sessionName }),
  });

  return parseJsonResponse<ThreadSummary>(response);
}

export async function deleteThread(threadId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/threads/${threadId}`, {
    method: "DELETE",
  });

  return parseJsonResponse<{ status: string; deleted: string }>(response);
}

export async function fetchWorkspaceOutline(workspaceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/outline`);
  return parseJsonResponse<WorkspaceOutlineContent>(response);
}

export async function fetchWorkspaceWorldview(workspaceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/worldview`);
  return parseJsonResponse<WorkspaceWorldviewContent>(response);
}

export async function fetchWorkspaceStoryline(workspaceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/storyline`);
  return parseJsonResponse<WorkspaceStorylineContent>(response);
}

export async function fetchWorkspaceStorylineGraph(workspaceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/storyline-graph`);
  return parseJsonResponse<WorkspaceStorylineGraphContent>(response);
}

export async function fetchWorkspaceDetailOutline(workspaceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/detail-outline`);
  return parseJsonResponse<WorkspaceDetailOutlineContent>(response);
}

export async function fetchWorkspaceCharacters(workspaceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/characters`);
  return parseJsonResponse<WorkspaceCharacterContent>(response);
}

export async function fetchWorkspaceNovel(workspaceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/novel`);
  return parseJsonResponse<WorkspaceNovelContent>(response);
}

export function workspaceNovelPdfUrl(workspaceId: string) {
  return `${API_BASE_URL}/api/workspaces/${workspaceId}/novel/export.pdf`;
}

export function workspaceNovelWordUrl(workspaceId: string) {
  return `${API_BASE_URL}/api/workspaces/${workspaceId}/novel/export-word.zip`;
}

export async function fetchThreadTraces(threadId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/threads/${threadId}/traces`);
  return parseJsonResponse<TraceRunSummary[]>(response);
}

export async function fetchTraceDetail(threadId: string, traceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/threads/${threadId}/traces/${traceId}`);
  return parseJsonResponse<TraceDetail>(response);
}

export async function deleteTrace(threadId: string, traceId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/threads/${threadId}/traces/${traceId}`, {
    method: "DELETE",
  });

  return parseJsonResponse<{ status: string; deleted: string }>(response);
}

export async function stopScreenplay(threadId: string, traceId: string) {
  // D-停止真生效：后端 request_user_stop（设 reason 标记）+ cancel_run_task（真 task.cancel）。
  // 幂等：trace 已结束也返回 200，task_cancelled=false。
  const response = await apiFetch(`${API_BASE_URL}/api/screenplay/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId, trace_id: traceId }),
  });

  return parseJsonResponse<{ status: string; trace_id: string; task_cancelled: boolean }>(response);
}

export async function fetchThreadCheckpoint(threadId: string): Promise<CheckpointState> {
  const response = await apiFetch(`${API_BASE_URL}/api/threads/${threadId}/checkpoint`);
  if (!response.ok) throw new Error("Failed to fetch checkpoint");
  return response.json();
}

export async function fetchStyles() {
  const response = await apiFetch(`${API_BASE_URL}/api/styles`);
  return parseJsonResponse<Style[]>(response);
}

export async function createStyle(name: string, metaStyle = "", storybuildingStyle = "", detailOutlineStyle = "", writingStyle = "") {
  const response = await apiFetch(`${API_BASE_URL}/api/styles`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      meta_style: metaStyle,
      storybuilding_style: storybuildingStyle,
      detail_outline_style: detailOutlineStyle,
      writing_style: writingStyle,
    }),
  });

  return parseJsonResponse<Style>(response);
}

export async function updateStyle(
  styleId: string,
  fields: { name?: string; meta_style?: string; storybuilding_style?: string; detail_outline_style?: string; writing_style?: string },
) {
  const response = await apiFetch(`${API_BASE_URL}/api/styles/${styleId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });

  return parseJsonResponse<Style>(response);
}

export async function optimizeStyle(styleType: string, content: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/styles/optimize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ style_type: styleType, content }),
  });

  return parseJsonResponse<{ optimized: string }>(response);
}

export async function deleteStyle(styleId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/styles/${styleId}`, {
    method: "DELETE",
  });

  return parseJsonResponse<{ status: string; deleted: string }>(response);
}

export async function activateStyle(workspaceId: string, styleId: string | null) {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/style`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ style_id: styleId }),
  });

  return parseJsonResponse<WorkspaceSummary>(response);
}

export async function fetchThreadOutline(threadId: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/threads/${threadId}/outline`);
  return parseJsonResponse<WorkspaceOutlineContent>(response);
}

export async function generateCharacter(payload: CharacterGenerateRequest) {
  const response = await apiFetch(`${API_BASE_URL}/api/character/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  return parseJsonResponse<CharacterGenerateResponse>(response);
}

// ── 数据闭环 E3：隐式反馈信号埋点（fire-and-forget，失败静默） ──

/**
 * 记录"用户复制了内容"信号（正信号）。
 * fire-and-forget：失败不影响用户操作，不抛异常。
 */
export function trackCopy(traceId: string, contentPreview = ""): void {
  if (!traceId) return;
  apiFetch(`${API_BASE_URL}/api/screenplay/copy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trace_id: traceId, content_preview: contentPreview.slice(0, 200) }),
  }).catch(() => { /* fire-and-forget */ });
}

/**
 * 记录"用户点了重试/重新生成"信号（负信号）。
 * fire-and-forget：失败不影响用户操作，不抛异常。
 */
export function trackRegenerate(traceId: string, contentPreview = ""): void {
  if (!traceId) return;
  apiFetch(`${API_BASE_URL}/api/screenplay/regenerate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trace_id: traceId, content_preview: contentPreview.slice(0, 200) }),
  }).catch(() => { /* fire-and-forget */ });
}
