// adapt-api.ts —— 进化循环 + 配置版本的 API 封装层（需求 §4.3）。
//
// 复用 monitor-api.ts 的 API_BASE_URL 约定（dev 直连 7789 / prod 同源）。
// SSE 用原生 EventSource（不能走 fetch），其余用统一 fetch 封装。

import { API_BASE_URL } from "./monitor-api";
import type {
  AdaptSessionDetail,
  AdaptSessionListItem,
  AdaptStartParams,
  AdaptStartResponse,
  AdaptStreamEvent,
  VersionDetail,
  VersionListResponse,
} from "./adapt-types";

async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE_URL}${path}`, init);
  if (!resp.ok) {
    throw new Error(`API ${resp.status}: ${path}`);
  }
  return (await resp.json()) as T;
}

// ── 触发（D6 可调参数）─────────────────────────────────────

export async function startAdapt(
  params: AdaptStartParams,
): Promise<AdaptStartResponse> {
  return apiJson<AdaptStartResponse>("/api/adapt/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
}

// ── 软停（D12）─────────────────────────────────────────────

export async function stopSession(sessionId: string): Promise<void> {
  await apiJson(`/api/adapt/sessions/${encodeURIComponent(sessionId)}/stop`, {
    method: "POST",
  });
}

// ── 查询 ───────────────────────────────────────────────────

export async function fetchSessions(params?: {
  limit?: number;
  offset?: number;
}): Promise<{ items: AdaptSessionListItem[]; total: number }> {
  const qs = new URLSearchParams();
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.offset) qs.set("offset", String(params.offset));
  const query = qs.toString();
  return apiJson(`/api/adapt/sessions${query ? `?${query}` : ""}`);
}

export async function fetchSessionDetail(
  sessionId: string,
): Promise<AdaptSessionDetail> {
  return apiJson<AdaptSessionDetail>(
    `/api/adapt/sessions/${encodeURIComponent(sessionId)}`,
  );
}

// ── SSE 订阅（D4）──────────────────────────────────────────

/**
 * 订阅 adapt session 的实时事件流（D4）。
 *
 * 返回一个 cleanup 函数：调用即断开 EventSource（D10：不重连）。
 * onEvent 收到 session_end / error 时，调用方应停止等待。
 */
export function subscribeAdaptStream(
  sessionId: string,
  onEvent: (event: AdaptStreamEvent) => void,
  onError?: (err: Event) => void,
): () => void {
  const url = `${API_BASE_URL}/api/adapt/sessions/${encodeURIComponent(sessionId)}/stream`;
  const source = new EventSource(url);

  source.onmessage = (msg) => {
    // 心跳注释行不会触发 onmessage；只有 data: 行会到这
    try {
      const parsed = JSON.parse(msg.data) as AdaptStreamEvent;
      onEvent(parsed);
      // 终态事件后主动关闭（后端也会关，双保险）
      if (parsed.type === "session_end" || parsed.type === "error") {
        source.close();
      }
    } catch {
      // 非 JSON（如保活注释的边缘情况），忽略
    }
  };

  source.onerror = (err) => {
    // D10：断连不重连。EventSource 默认会重连，这里主动 close。
    source.close();
    onError?.(err);
  };

  return () => source.close();
}

// ── 配置版本谱系（D8）──────────────────────────────────────

export async function fetchVersions(params?: {
  limit?: number;
  offset?: number;
}): Promise<VersionListResponse> {
  const qs = new URLSearchParams();
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.offset) qs.set("offset", String(params.offset));
  const query = qs.toString();
  return apiJson(`/api/versions${query ? `?${query}` : ""}`);
}

export async function fetchVersionDetail(
  version: number,
): Promise<VersionDetail> {
  return apiJson<VersionDetail>(`/api/versions/${version}`);
}
