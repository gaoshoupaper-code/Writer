import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { evoSseStream } from "@/lib/stream";
import {
  getEvolveSessions,
  startEvolve,
  stopEvolve,
  getEvaluatedTraces,
  type EvolveSession,
  type EvalSession,
} from "@/lib/api";

/**
 * 进化核心工作流（设计文档：核心工作区大 tab）。
 *
 * 工作流：
 * 1. 选一条已评估的 trace（作为进化输入）
 * 2. 启动进化 → SSE 实时流看 Agent 执行步骤
 * 3. 进化完成（pending_review）→ 审查 → 发布或丢弃
 *
 * 左侧：历史进化会话列表
 * 右侧：当前会话详情 + 实时日志流
 */
export default function EvolvePage() {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<EvolveSession[]>([]);
  const [evaluatedTraces, setEvaluatedTraces] = useState<EvalSession[]>([]);
  const [selectedSession, setSelectedSession] = useState<EvolveSession | null>(null);
  const [selectedTraceId, setSelectedTraceId] = useState("");
  const [starting, setStarting] = useState(false);
  const [liveLogs, setLiveLogs] = useState<{ type: string; message?: string; [k: string]: any }[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamingSessionId, setStreamingSessionId] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const streamCancelRef = useRef<(() => void) | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [sess, evals] = await Promise.all([
        getEvolveSessions(30).catch(() => ({ sessions: [], total: 0 })),
        getEvaluatedTraces(50).catch(() => ({ traces: [], total: 0 })),
      ]);
      setSessions(sess.sessions);
      setEvaluatedTraces(evals.traces);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取数据失败");
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 10000);
    return () => {
      clearInterval(timer);
      if (streamCancelRef.current) streamCancelRef.current();
    };
  }, [refresh]);

  async function handleStart() {
    if (!selectedTraceId) {
      toast.error("请先选择一条已评估的 trace");
      return;
    }
    setStarting(true);
    setLiveLogs([]);
    try {
      const resp = await startEvolve(selectedTraceId);
      toast.success(`进化已启动：${resp.session_id.slice(0, 8)}`);
      // 立即订阅 SSE 流
      setStreaming(true);
      setStreamingSessionId(resp.session_id);
      subscribeStream(resp.session_id);
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "启动进化失败");
    } finally {
      setStarting(false);
    }
  }

  async function handleStop() {
    if (!streamingSessionId) return;
    if (!window.confirm("确定停止？Agent 正在执行的改动可能不完整。")) return;
    setStopping(true);
    try {
      await stopEvolve(streamingSessionId);
      toast.success("已请求停止进化");
      // 断开前端 SSE 订阅；后端 cancel_run 会推 error 帧但前端可能已主动断开
      streamCancelRef.current?.();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "停止失败");
    } finally {
      setStopping(false);
    }
  }

  async function subscribeStream(sessionId: string) {
    if (streamCancelRef.current) streamCancelRef.current();
    let cancelled = false;
    streamCancelRef.current = () => { cancelled = true; };

    try {
      const gen = evoSseStream(`/api/evolve/sessions/${sessionId}/stream`, { method: "GET" });
      for await (const frame of gen) {
        if (cancelled) break;
        if (frame.type === "heartbeat") continue;
        setLiveLogs((prev) => [...prev, frame]);
        if (frame.type === "end" || frame.type === "error") {
          setStreaming(false);
          setStreamingSessionId(null);
          refresh();
          break;
        }
      }
    } catch (err) {
      if (!cancelled) {
        toast.error(err instanceof Error ? err.message : "实时流中断");
        setStreaming(false);
      }
    }
  }

  return (
    <div className="evolve-page">
      <header className="page-header">
        <h1>进化</h1>
        <p className="page-desc">基于评估结果进化 Agent（选已评估 trace → 启动 → 审查 → 发布）</p>
      </header>

      <div className="evolve-layout">
        {/* 左：历史会话 */}
        <aside className="evolve-sidebar">
          <h2 className="sidebar-title">历史会话（{sessions.length}）</h2>
          <div className="session-list">
            {sessions.map((s) => (
              <div
                key={s.session_id}
                className={`session-item ${selectedSession?.session_id === s.session_id ? "active" : ""}`}
                onClick={() => {
                  setSelectedSession(s);
                  setLiveLogs([]);
                  if (s.status === "running") {
                    setStreaming(true);
                    setStreamingSessionId(s.session_id);
                    subscribeStream(s.session_id);
                  }
                }}
              >
                <div className="session-item-head">
                  <span className={`session-status ${s.status}`}>{statusLabel(s.status)}</span>
                  <span className="session-time">{formatTime(s.created_at)}</span>
                </div>
                <div className="session-item-meta">
                  <span>基线 {s.baseline_score?.toFixed(2) ?? "—"}</span>
                  <span>候选 {s.candidate_score?.toFixed(2) ?? "—"}</span>
                </div>
              </div>
            ))}
            {sessions.length === 0 && <div className="monitor-empty">暂无进化记录</div>}
          </div>
        </aside>

        {/* 右：工作区 */}
        <main className="evolve-main">
          {/* 启动区 */}
          <section className="evolve-start">
            <h3>启动新进化</h3>
            <div className="evolve-start-form">
              <select
                className="evolve-select"
                value={selectedTraceId}
                onChange={(e) => setSelectedTraceId(e.target.value)}
                disabled={starting}
              >
                <option value="">选择已评估的 trace…</option>
                {evaluatedTraces.map((e) => (
                  <option key={e.eval_id} value={e.trace_id}>
                    {e.trace_id.slice(0, 12)}…（{e.status}）
                  </option>
                ))}
              </select>
              <button
                className="config-button primary"
                onClick={handleStart}
                disabled={starting || !selectedTraceId}
              >
                {starting ? "启动中…" : "启动进化"}
              </button>
            </div>
          </section>

          {/* 实时日志 / 会话详情 */}
          {(liveLogs.length > 0 || selectedSession || streaming) && (
            <section className="evolve-detail">
              {streaming && (
                <div className="streaming-badge">
                  <span className="pulse" /> 实时流中…
                  <button
                    className="config-button danger"
                    onClick={handleStop}
                    disabled={stopping}
                    style={{ marginLeft: 12 }}
                  >
                    {stopping ? "停止中…" : "停止"}
                  </button>
                </div>
              )}
              {liveLogs.length > 0 && (
                <div className="log-stream">
                  {liveLogs.map((log, i) => (
                    <div key={i} className={`log-line log-${log.type}`}>
                      <span className="log-type">{log.type}</span>
                      {log.message && <span className="log-msg">{log.message}</span>}
                      {log.tool && <span className="log-tool">🔧 {log.tool}</span>}
                    </div>
                  ))}
                </div>
              )}
              {selectedSession && !streaming && (
                <div className="session-detail">
                  <h3>会话 {selectedSession.session_id.slice(0, 8)}</h3>
                  <div className="detail-grid">
                    <div><label>状态</label><span className={`session-status ${selectedSession.status}`}>{statusLabel(selectedSession.status)}</span></div>
                    <div><label>阶段</label><span>{selectedSession.phase || "—"}</span></div>
                    <div><label>基线分</label><span>{selectedSession.baseline_score?.toFixed(2) ?? "—"}</span></div>
                    <div><label>候选分</label><span>{selectedSession.candidate_score?.toFixed(2) ?? "—"}</span></div>
                  </div>
                  {selectedSession.baseline_trace && (
                    <button className="link-button" onClick={() => navigate(`/traces/${selectedSession.baseline_trace}`)}>
                      查看基线 trace →
                    </button>
                  )}
                  {selectedSession.candidate_trace && (
                    <button className="link-button" onClick={() => navigate(`/traces/${selectedSession.candidate_trace}`)}>
                      查看候选 trace →
                    </button>
                  )}
                  {/* 审查入口（按状态区分） */}
                  {(selectedSession.status === "pending_review" ||
                    selectedSession.status === "published" ||
                    selectedSession.status === "discarded" ||
                    selectedSession.status === "failed") && (
                    <div className="review-actions">
                      <button
                        className="config-button primary"
                        onClick={() => navigate(`/evolve/${selectedSession.session_id}/review`)}
                      >
                        {selectedSession.status === "pending_review" ? "🔍 审查报告" : "查看报告"}
                      </button>
                    </div>
                  )}
                </div>
              )}
            </section>
          )}
        </main>
      </div>
    </div>
  );
}

function statusLabel(s: string): string {
  const map: Record<string, string> = {
    running: "运行中", done: "完成", failed: "失败",
    pending_review: "待审查", published: "已发布", discarded: "已丢弃",
    cancelled: "已停止",
  };
  return map[s] ?? s;
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}
