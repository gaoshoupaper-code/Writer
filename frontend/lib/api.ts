import type { CharacterGenerateRequest, CharacterGenerateResponse, CheckpointState, InitResponse, Style, ThreadSummary, TraceDetail, TraceRunSummary, WorkspaceBootstrapResponse, WorkspaceCharacterContent, WorkspaceDetailOutlineContent, WorkspaceNovelContent, WorkspaceOutlineContent, WorkspaceStorylineContent, WorkspaceWorldviewContent, WorkspaceStorylineGraphContent, WorkspaceSummary } from "./types";

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:7788";

/**
 * 统一 fetch 封装：
 * - credentials:"include"：携带 Session Cookie（D4）。
 * - 401 拦截：跳转到 /login（仅浏览器侧，SSR 跳过）。
 *
 * 所有 API 调用都应走这个封装，保证认证态一致。
 */
export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(input, { ...init, credentials: "include" });
  if (response.status === 401 && typeof window !== "undefined") {
    // 未登录/会话失效 → 跳登录页（避免在 /login 自身循环）
    if (!window.location.pathname.startsWith("/login") && !window.location.pathname.startsWith("/register")) {
      window.location.href = "/login";
    }
  }
  return response;
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
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
  workspace_count: number;
  workspace_quota: number;
};

export async function fetchMyProfile() {
  return apiJson<MyProfile>(`${API_BASE_URL}/api/me`);
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
  outline_name: string;
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
  return apiJson<{ workspace_id: string; outline_name: string; markdown: string }>(
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

export async function createWorkspace(outlineName: string) {
  const response = await apiFetch(`${API_BASE_URL}/api/workspaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ outline_name: outlineName }),
  });

  return parseJsonResponse<WorkspaceSummary>(response);
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
