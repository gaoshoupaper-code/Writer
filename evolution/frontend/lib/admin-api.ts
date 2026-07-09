// 管理后台 API 封装（积分制 Phase 4）。
// 所有请求走 evolution 后端 /api/admin/* 代理路由（带 SSO cookie）。

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE_URL}${path}`, {
    credentials: "include",
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    let detail = text;
    try {
      detail = JSON.parse(text).detail ?? text;
    } catch {
      /* keep raw text */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return (await resp.json()) as T;
}

// ── 类型 ────────────────────────────────────

export type AdminUser = {
  user_id: string;
  username: string;
  is_admin: boolean;
  is_super_admin: boolean;
  disabled: boolean;
  has_api_key: boolean;
  credits_balance: number;
  workspace_count: number;
  created_at: string;
};

export type InviteCode = {
  code: string;
  created_at: string;
  is_admin_code: boolean;
  granted_credits: number;
  used: boolean;
  used_by: string | null;
  used_at: string | null;
  revoked_at: string | null;
};

export type CreditTransaction = {
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
};

// ── 用户管理 ─────────────────────────────────

export const fetchUsers = () => apiFetch<AdminUser[]>("/api/admin/users");

export const updateUser = (userId: string, payload: { disabled?: boolean; reset_password?: string }) =>
  apiFetch<Record<string, unknown>>(`/api/admin/users/${userId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });

export const resetPassword = (userId: string) =>
  apiFetch<{ status: string; temp_password: string }>(`/api/admin/users/${userId}/reset-password`, {
    method: "POST",
  });

export const adjustCredits = (userId: string, amount: number, note: string) =>
  apiFetch<{ status: string; balance: number }>(`/api/admin/users/${userId}/credits`, {
    method: "POST",
    body: JSON.stringify({ amount, note }),
  });

export const fetchUserWorkspaces = (userId: string) =>
  apiFetch<Record<string, unknown>[]>(`/api/admin/users/${userId}/workspaces`);

// ── 邀请码管理 ───────────────────────────────

export const fetchInviteCodes = () => apiFetch<InviteCode[]>("/api/admin/invite-codes");

export const createInviteCodes = (count: number, grantedCredits: number) =>
  apiFetch<string[]>("/api/admin/invite-codes", {
    method: "POST",
    body: JSON.stringify({ count, granted_credits: grantedCredits }),
  });

export const revokeInviteCode = (code: string) =>
  apiFetch<Record<string, unknown>>(`/api/admin/invite-codes/${code}`, { method: "DELETE" });

// ── 积分流水 ─────────────────────────────────

export const fetchAllTransactions = (limit = 100) =>
  apiFetch<CreditTransaction[]>(`/api/admin/credits/transactions?limit=${limit}`);

export const fetchUserTransactions = (userId: string, limit = 50) =>
  apiFetch<CreditTransaction[]>(`/api/admin/users/${userId}/credits/transactions?limit=${limit}`);

// ── 当前用户余额 ─────────────────────────────

export const fetchMyCredits = () => apiFetch<{ balance: number }>("/api/admin/me/credits");

// ── 积分暗调参数（AD11）──────────────────────

export type CreditConfigItem = {
  value: string;
  description: string;
  updated_at: string;
};

export const fetchCreditsConfig = () =>
  apiFetch<Record<string, CreditConfigItem>>("/api/admin/credits/config");

export const updateCreditsConfig = (key: string, value: string) =>
  apiFetch<Record<string, unknown>>(`/api/admin/credits/config/${key}`, {
    method: "PUT",
    body: JSON.stringify({ value }),
  });
