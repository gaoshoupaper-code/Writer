/**
 * 进化端 API 客户端（三功能解耦后重写，决策 S7/S8/S9）。
 *
 * 三功能：
 *   ① 单次测试（tests-api.ts，已就绪）
 *   ② 评估 Agent：/api/eval-agent/*
 *   ③ 进化 Agent：/api/evolve/*（精简为方案→执行，强前置需已评估 trace）
 *
 * 发版/丢弃：/api/evolve/sessions/{id}/publish | /discard
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "";

// ── 进化 session（功能③）──────────────────────────────────────

export interface EvolveSession {
  session_id: string;
  case_id: string;
  status: string; // running/pending_review/published/discarded/failed
  trace_id?: string | null;
  baseline_trace?: string | null; // 向后兼容（旧字段）
  design_doc_path?: string | null;
  change_log_path?: string | null;
  eval_ref?: string | null; // 关联的评估 eval_id
  created_at: string;
  updated_at: string | null;
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
  | EvolveErrorEvent
  | EvolveEndEvent
  | EvolveStartEvent
  | EvolveHeartbeat;

/**
 * 启动进化（强前置：trace 必须已被评估 Agent 评估过，S8/T2）。
 */
export async function startEvolve(traceId: string): Promise<{ session_id: string; eval_id: string }> {
  const resp = await fetch(`${API_BASE}/api/evolve/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trace_id: traceId }),
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

/** 发版（固化新 Agent 版本，S9/S12）。 */
export async function publishSession(
  sessionId: string,
): Promise<{ status: string; snapshot_version: number; source_commit: string; notified: boolean }> {
  const resp = await fetch(`${API_BASE}/api/evolve/sessions/${sessionId}/publish`, {
    method: "POST",
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`发版失败: ${resp.status} ${text}`);
  }
  return resp.json();
}

/** 丢弃（回退 working 区，S9）。 */
export async function discardSession(
  sessionId: string,
): Promise<{ status: string; reset_to: string }> {
  const resp = await fetch(`${API_BASE}/api/evolve/sessions/${sessionId}/discard`, {
    method: "POST",
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`丢弃失败: ${resp.status} ${text}`);
  }
  return resp.json();
}

/**
 * 订阅进化 session 的 SSE 实时流。返回 cleanup 函数。
 */
export function subscribeEvolveStream(
  sessionId: string,
  onEvent: (evt: EvolveStreamEvent) => void,
  onError?: () => void,
): () => void {
  return _subscribeStream(
    `${API_BASE}/api/evolve/sessions/${sessionId}/stream`,
    onEvent,
    onError,
  );
}

// ── 评估集 case（单次测试 + 进化共用，数据集选择）──────────────

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

// ── 评估 Agent session（功能②）────────────────────────────────

export interface EvalSession {
  eval_id: string;
  trace_id: string;
  agent_version_type: string | null;
  agent_version_id: number | null;
  status: string; // running/done/failed
  scores: Record<string, unknown> | null;
  findings: EvalFinding[] | null;
  report_md: string | null;
  created_at: string;
  updated_at: string;
}

export interface EvalFinding {
  id?: string; // 稳定标识 f01/f02…（write_eval_report 工具强制生成，进化端 evidence_ref 引用）
  dimension: string;
  severity: string; // high/medium/low
  evidence_type: string; // 实证/推断
  finding: string;
  evidence: string;
}

export type EvalStreamEvent = EvolveStreamEvent; // 事件结构相同，复用类型

/** 启动评估（传 trace_id，S7）。 */
export async function startEval(traceId: string): Promise<{ eval_id: string }> {
  const resp = await fetch(`${API_BASE}/api/eval-agent/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trace_id: traceId }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`启动评估失败: ${resp.status} ${text}`);
  }
  return resp.json();
}

export async function fetchEvalSessions(traceId?: string): Promise<EvalSession[]> {
  const params = traceId ? `?trace_id=${encodeURIComponent(traceId)}` : "";
  const resp = await fetch(`${API_BASE}/api/eval-agent/sessions${params}`);
  if (!resp.ok) throw new Error(`获取评估列表失败: ${resp.status}`);
  const data = await resp.json();
  return data.sessions || [];
}

export async function fetchEvalDetail(evalId: string): Promise<EvalSession> {
  const resp = await fetch(`${API_BASE}/api/eval-agent/sessions/${evalId}`);
  if (!resp.ok) throw new Error(`获取评估详情失败: ${resp.status}`);
  return resp.json();
}

/** 列已评估的 trace（进化入口「选已评估 trace」用，S8 强前置）。 */
export async function fetchEvaluatedTraces(): Promise<EvalSession[]> {
  const resp = await fetch(`${API_BASE}/api/eval-agent/evaluated-traces`);
  if (!resp.ok) throw new Error(`获取已评估 trace 列表失败: ${resp.status}`);
  const data = await resp.json();
  return data.traces || [];
}

/** 订阅评估 session 的 SSE 实时流。返回 cleanup 函数。 */
export function subscribeEvalStream(
  evalId: string,
  onEvent: (evt: EvalStreamEvent) => void,
  onError?: () => void,
): () => void {
  return _subscribeStream(
    `${API_BASE}/api/eval-agent/sessions/${evalId}/stream`,
    onEvent,
    onError,
  );
}

// ── 通用 SSE 订阅内核 ─────────────────────────────────────────

function _subscribeStream(
  url: string,
  onEvent: (evt: EvolveStreamEvent) => void,
  onError?: () => void,
): () => void {
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
