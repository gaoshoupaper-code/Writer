// tests-api.ts —— 手动测试 API 客户端（决策 D-Q16）
//
// 对接后端 /api/tests/* + /api/evolve/cases/* 端点：
//   POST /api/tests                  发起测试
//   GET  /api/tests                  测试记录列表（状态 tab + 分页）
//   GET  /api/tests/{id}             单条详情
//   POST /api/tests/{id}/retry       重试失败测试
//   POST /api/tests/{id}/stop        停止运行中的测试（super-step 边界停）
//   DELETE /api/tests/{id}           删除测试记录 + 关联 trace 数据
//   GET  /api/tests/agents           可选 Agent 版本（working + 快照）
//   GET  /api/evolve/cases           数据集列表（带 title）
//   GET  /api/evolve/cases/{id}      数据集详情（demand.md 全文）
//
// 沿用 monitor-api.ts 的封装风格（apiJson<T> + NEXT_PUBLIC_API_BASE_URL）。

import type { CaseSummary, CaseDetail } from "./evolve-api";

// 重新导出数据集类型，供页面从 tests-api 统一引入
export type { CaseSummary, CaseDetail };

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

/** 统一 fetch 尛装：拼接 API_BASE_URL，解析 JSON，统一错误。 */
async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE_URL}${path}`, init);
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`API ${resp.status}: ${path}${text ? ` — ${text}` : ""}`);
  }
  return (await resp.json()) as T;
}

// ── 类型 ──────────────────────────────────────────────────────

export type TestStatus = "pending" | "running" | "done" | "failed" | "cancelled";
export type VersionType = "working" | "snapshot";

export interface TestRecord {
  test_id: string;
  case_id: string;
  version_type: VersionType;
  version_id: number | null;
  trace_id: string | null;
  task_id: string | null;
  status: TestStatus;
  error: string | null;
  retry_of: string | null;
  created_at: string;
}

export interface TestAgent {
  type: VersionType;
  version?: number;
  label: string;
  source_commit: string;
  change_summary?: string;
}

export interface StartTestRequest {
  case_id: string;
  version_type: VersionType;
  version_id: number | null;
}

export interface StartTestResponse {
  test_id: string;
  status: string;
}

export interface TestListResponse {
  tests: TestRecord[];
  total: number;
  page: number;
  page_size: number;
}

// ── 数据集（复用 evolve-api 的类型，但走本 lib 的 fetch 封装以统一 base url）──

export async function fetchCases(): Promise<CaseSummary[]> {
  return apiJson<{ cases: CaseSummary[] }>(`/api/evolve/cases`).then((d) => d.cases || []);
}

export async function fetchCaseDetail(caseId: string): Promise<CaseDetail> {
  return apiJson<CaseDetail>(`/api/evolve/cases/${encodeURIComponent(caseId)}`);
}

// ── Agent 版本 ────────────────────────────────────────────────

export async function fetchTestAgents(): Promise<TestAgent[]> {
  return apiJson<{ agents: TestAgent[] }>(`/api/tests/agents`).then((d) => d.agents || []);
}

// ── 测试记录 ──────────────────────────────────────────────────

export async function fetchTests(params?: {
  status?: string;
  page?: number;
  page_size?: number;
}): Promise<TestListResponse> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.page) qs.set("page", String(params.page));
  if (params?.page_size) qs.set("page_size", String(params.page_size));
  const query = qs.toString();
  return apiJson<TestListResponse>(`/api/tests${query ? `?${query}` : ""}`);
}

export async function fetchTestDetail(testId: string): Promise<TestRecord> {
  return apiJson<TestRecord>(`/api/tests/${encodeURIComponent(testId)}`);
}

export async function startTest(req: StartTestRequest): Promise<StartTestResponse> {
  return apiJson<StartTestResponse>(`/api/tests`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

export async function retryTest(testId: string): Promise<StartTestResponse> {
  return apiJson<StartTestResponse>(`/api/tests/${encodeURIComponent(testId)}/retry`, {
    method: "POST",
  });
}

/** 停止运行中的测试（边界停，非立即）。后端：POST /api/tests/{id}/stop。 */
export async function stopTest(testId: string): Promise<{ status: string; test_id: string }> {
  return apiJson<{ status: string; test_id: string }>(
    `/api/tests/${encodeURIComponent(testId)}/stop`,
    { method: "POST" },
  );
}

/** 删除测试记录 + 关联 trace 数据（仅终态可删）。后端：DELETE /api/tests/{id}。 */
export async function deleteTest(
  testId: string,
): Promise<{ status: string; deleted: string; trace_id: string | null; trace_removed: boolean }> {
  return apiJson<{
    status: string;
    deleted: string;
    trace_id: string | null;
    trace_removed: boolean;
  }>(`/api/tests/${encodeURIComponent(testId)}`, { method: "DELETE" });
}
