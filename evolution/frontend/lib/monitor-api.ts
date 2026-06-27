// monitor-api.ts —— evolution 监测端 API 封装层（D10）
//
// 所有 evolution 端点调用的唯一入口。同 frontend/api.ts 的模式但独立：
// - API_BASE_URL 由环境变量区分 dev/prod（D11）
//   dev: http://localhost:7789（直连，避开 EventSource+rewrites 坑）
//   prod: 同源（空字符串，StaticFiles 托管）
// - 无鉴权（监测端无登录），不加 credentials
//
// 设计依据：设计文档 D10/D11。

import type {
  ActiveRun,
  FailurePattern,
  SkillStat,
  StatsOverview,
  TimelinePoint,
  TraceDetail,
  TraceListItem,
} from "./types";

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

/**
 * 统一 fetch 封装。拼接 API_BASE_URL，解析 JSON，统一错误。
 */
async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE_URL}${path}`, init);
  if (!resp.ok) {
    throw new Error(`API ${resp.status}: ${path}`);
  }
  return (await resp.json()) as T;
}

// ── trace 列表/详情（现有 evolution traces.py 端点）──

export async function fetchTraces(params?: {
  workspace?: string;
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<TraceListItem[]> {
  const qs = new URLSearchParams();
  if (params?.workspace) qs.set("workspace", params.workspace);
  if (params?.status) qs.set("status", params.status);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.offset) qs.set("offset", String(params.offset));
  const query = qs.toString();
  return apiJson<TraceListItem[]>(`/api/traces${query ? `?${query}` : ""}`);
}

export async function fetchTraceDetail(traceId: string): Promise<TraceDetail> {
  return apiJson<TraceDetail>(`/api/traces/${encodeURIComponent(traceId)}`);
}

export async function deleteTrace(traceId: string): Promise<void> {
  await apiJson(`/api/traces/${encodeURIComponent(traceId)}`, { method: "DELETE" });
}

// ── 活跃大盘（D7 富化端点）──

export async function fetchActiveRuns(): Promise<ActiveRun[]> {
  return apiJson<ActiveRun[]>("/api/active-runs");
}

// ── SSE 流 URL（EventSource 用，不走 fetch）──

/**
 * 构建 trace 实时 SSE 的完整 URL（D9）。
 * EventSource 不能走 fetch 封装，需直接拼 URL 传给 new EventSource()。
 */
export function traceStreamUrl(traceId: string): string {
  return `${API_BASE_URL}/api/traces/${encodeURIComponent(traceId)}/stream`;
}

// ── 探活：判断 trace 是否在活跃集合（D5 分流用）──

export async function isTraceActive(traceId: string): Promise<boolean> {
  try {
    const active = await fetchActiveRuns();
    return active.some((r) => r.trace_id === traceId);
  } catch {
    return false;
  }
}

// ── 宏观统计（/api/stats/*）──

export async function fetchStatsOverview(params?: {
  workspace?: string;
  hours?: number;
}): Promise<StatsOverview> {
  const qs = new URLSearchParams();
  if (params?.workspace) qs.set("workspace", params.workspace);
  if (params?.hours) qs.set("hours", String(params.hours));
  const query = qs.toString();
  return apiJson<StatsOverview>(`/api/stats/overview${query ? `?${query}` : ""}`);
}

export async function fetchSkillStats(params?: {
  workspace?: string;
  hours?: number;
  top?: number;
}): Promise<SkillStat[]> {
  const qs = new URLSearchParams();
  if (params?.workspace) qs.set("workspace", params.workspace);
  if (params?.hours) qs.set("hours", String(params.hours));
  if (params?.top) qs.set("top", String(params.top));
  const query = qs.toString();
  return apiJson<SkillStat[]>(`/api/stats/skills${query ? `?${query}` : ""}`);
}

export async function fetchTimeline(params?: {
  workspace?: string;
  hours?: number;
  bucket_hours?: number;
}): Promise<TimelinePoint[]> {
  const qs = new URLSearchParams();
  if (params?.workspace) qs.set("workspace", params.workspace);
  if (params?.hours) qs.set("hours", String(params.hours));
  if (params?.bucket_hours) qs.set("bucket_hours", String(params.bucket_hours));
  const query = qs.toString();
  return apiJson<TimelinePoint[]>(`/api/stats/timeline${query ? `?${query}` : ""}`);
}

export async function fetchFailureStats(params?: {
  workspace?: string;
  hours?: number;
  top?: number;
}): Promise<FailurePattern[]> {
  const qs = new URLSearchParams();
  if (params?.workspace) qs.set("workspace", params.workspace);
  if (params?.hours) qs.set("hours", String(params.hours));
  if (params?.top) qs.set("top", String(params.top));
  const query = qs.toString();
  return apiJson<FailurePattern[]>(`/api/stats/failures${query ? `?${query}` : ""}`);
}
