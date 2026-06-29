/**
 * 进化 Agent API 客户端（替换旧 adapt-api）。
 *
 * 对接后端 /api/evolve/* 端点：
 *   POST /evolve/start          启动一次进化
 *   GET  /evolve/sessions       session 列表
 *   GET  /evolve/sessions/{id}  session 详情
 *   GET  /evolve/sessions/{id}/stream  SSE 实时流
 *   GET  /evolve/cases          评估集 case 列表
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "";

export interface EvolveSession {
  session_id: string;
  case_id: string;
  status: string; // running/done/failed
  baseline_trace: string | null;
  candidate_trace: string | null;
  baseline_score: number | null;
  candidate_score: number | null;
  report_json: string | null;
  report?: EvolveReport | null;
  created_at: string;
  updated_at: string | null;
}

export interface EvolveReport {
  content: string;
  baseline_score: number;
  candidate_score: number;
  delta: number;
  improved: boolean;
  baseline_trace: string;
  candidate_trace: string;
}

export interface EvolveStepEvent {
  type: "step";
  tool: string;
  status: string; // running/done/failed/blocked
  [key: string]: unknown;
}

export interface EvolveLogEvent {
  type: "log";
  message: string;
}

export interface EvolveReportEvent {
  type: "report";
  report: EvolveReport;
}

export interface EvolveErrorEvent {
  type: "error";
  reason: string;
}

export interface EvolveEndEvent {
  type: "end";
  outcome: string;
}

export interface EvolveStartEvent {
  type: "start";
  session_id: string;
}

export interface EvolveHeartbeat {
  type: "heartbeat";
}

export type EvolveStreamEvent =
  | EvolveStepEvent
  | EvolveLogEvent
  | EvolveReportEvent
  | EvolveErrorEvent
  | EvolveEndEvent
  | EvolveStartEvent
  | EvolveHeartbeat;

export async function startEvolve(caseId: string): Promise<{ session_id: string }> {
  const resp = await fetch(`${API_BASE}/api/evolve/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ case: caseId }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`启动进化失败: ${resp.status} ${text}`);
  }
  return resp.json();
}

export async function fetchSessions(): Promise<EvolveSession[]> {
  const resp = await fetch(`${API_BASE}/api/evolve/sessions`);
  if (!resp.ok) throw new Error(`获取 session 列表失败: ${resp.status}`);
  const data = await resp.json();
  return data.sessions || [];
}

export async function fetchSessionDetail(sessionId: string): Promise<EvolveSession> {
  const resp = await fetch(`${API_BASE}/api/evolve/sessions/${sessionId}`);
  if (!resp.ok) throw new Error(`获取 session 详情失败: ${resp.status}`);
  return resp.json();
}

export interface CaseSummary {
  case_id: string;
  title: string;
}

export interface CaseDetail {
  case_id: string;
  title: string;
  demand_md: string;
}

export async function fetchCases(): Promise<CaseSummary[]> {
  const resp = await fetch(`${API_BASE}/api/evolve/cases`);
  if (!resp.ok) throw new Error(`获取评估集失败: ${resp.status}`);
  const data = await resp.json();
  return data.cases || [];
}

export async function fetchCaseDetail(caseId: string): Promise<CaseDetail> {
  const resp = await fetch(`${API_BASE}/api/evolve/cases/${encodeURIComponent(caseId)}`);
  if (!resp.ok) throw new Error(`获取评估集详情失败: ${resp.status}`);
  return resp.json();
}

/**
 * 订阅 session 的 SSE 实时流。
 * 返回 cleanup 函数。
 */
export function subscribeEvolveStream(
  sessionId: string,
  onEvent: (evt: EvolveStreamEvent) => void,
  onError?: () => void,
): () => void {
  const url = `${API_BASE}/api/evolve/sessions/${sessionId}/stream`;
  const controller = new AbortController();

  fetch(url, { signal: controller.signal })
    .then(async (resp) => {
      if (!resp.ok || !resp.body) {
        onError?.();
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          const trimmed = line.trim();
          if (trimmed.startsWith("data: ")) {
            try {
              const evt = JSON.parse(trimmed.slice(6));
              onEvent(evt);
            } catch {
              // 忽略解析失败的事件
            }
          }
        }
      }
    })
    .catch(() => {
      if (!controller.signal.aborted) onError?.();
    });

  return () => controller.abort();
}
