import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { evoSseStream } from "@/lib/stream";
import {
  getEvalSessions,
  getEvalSession,
  startEval,
  getTraces,
  type EvalSession,
  type TraceListItem,
} from "@/lib/api";

/**
 * 评估页（设计文档：核心工作区大 tab）。
 *
 * 工作流：
 * 1. 选一条 trace（从最近的 trace 列表选）
 * 2. 启动评估 → SSE 实时流看评估 Agent 执行
 * 3. 完成后看评估报告（分数 + 问题清单）
 */
export default function EvaluationPage() {
  const navigate = useNavigate();
  const [evals, setEvals] = useState<EvalSession[]>([]);
  const [traces, setTraces] = useState<TraceListItem[]>([]);
  const [selectedEval, setSelectedEval] = useState<EvalSession | null>(null);
  const [selectedTraceId, setSelectedTraceId] = useState("");
  const [starting, setStarting] = useState(false);
  const [liveLogs, setLiveLogs] = useState<any[]>([]);
  const [streaming, setStreaming] = useState(false);
  const streamCancelRef = useRef<(() => void) | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [es, tr] = await Promise.all([
        getEvalSessions(30).catch(() => ({ sessions: [], total: 0 })),
        getTraces({ limit: 50 }).catch(() => ({ items: [], total: 0, limit: 50, offset: 0 })),
      ]);
      setEvals(es.sessions);
      // 排除进化端自观测 trace——评估 Agent 只评估创作 Agent 的 trace，
      // 不能评估自己（evolution_eval）或进化 Agent（evolution_evolve）的录像。
      setTraces(
        tr.items.filter(
          (t) => t.run_purpose !== "evolution_eval" && t.run_purpose !== "evolution_evolve",
        ),
      );
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
      toast.error("请先选择一条 trace");
      return;
    }
    setStarting(true);
    setLiveLogs([]);
    try {
      const resp = await startEval(selectedTraceId);
      toast.success(`评估已启动：${resp.eval_id.slice(0, 8)}`);
      setStreaming(true);
      subscribeStream(resp.eval_id);
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "启动评估失败");
    } finally {
      setStarting(false);
    }
  }

  async function subscribeStream(evalId: string) {
    if (streamCancelRef.current) streamCancelRef.current();
    let cancelled = false;
    streamCancelRef.current = () => { cancelled = true; };

    try {
      const gen = evoSseStream(`/api/eval-agent/sessions/${evalId}/stream`, { method: "GET" });
      for await (const frame of gen) {
        if (cancelled) break;
        if (frame.type === "heartbeat") continue;
        setLiveLogs((prev) => [...prev, frame]);
        if (frame.type === "end" || frame.type === "error") {
          setStreaming(false);
          // 拉取最新评估详情（含报告），确保 selectedEval 更新为带 report_md 的完整数据
          try {
            const detail = await getEvalSession(evalId);
            setSelectedEval(detail);
          } catch {
            // 详情拉取失败时退回列表刷新
          }
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
        <h1>评估</h1>
        <p className="page-desc">对 trace 跑 LLM-judge 评估（内容维度 + subagent 维度打分）</p>
      </header>

      <div className="evolve-layout">
        <aside className="evolve-sidebar">
          <h2 className="sidebar-title">历史评估（{evals.length}）</h2>
          <div className="session-list">
            {evals.map((e) => (
              <div
                key={e.eval_id}
                className={`session-item ${selectedEval?.eval_id === e.eval_id ? "active" : ""}`}
                onClick={() => {
                  setSelectedEval(e);
                  setLiveLogs([]);
                  if (e.status === "running") {
                    setStreaming(true);
                    subscribeStream(e.eval_id);
                  }
                }}
              >
                <div className="session-item-head">
                  <span className={`session-status ${e.status}`}>{evalStatusLabel(e.status)}</span>
                  <span className="session-time">{formatTime(e.created_at)}</span>
                </div>
                <div className="session-item-meta">
                  <span>{e.trace_id.slice(0, 12)}…</span>
                </div>
              </div>
            ))}
            {evals.length === 0 && <div className="monitor-empty">暂无评估记录</div>}
          </div>
        </aside>

        <main className="evolve-main">
          <section className="evolve-start">
            <h3>启动新评估</h3>
            <div className="evolve-start-form">
              <select
                className="evolve-select"
                value={selectedTraceId}
                onChange={(e) => setSelectedTraceId(e.target.value)}
                disabled={starting}
              >
                <option value="">选择 trace…</option>
                {traces.map((t) => (
                  <option key={t.trace_id} value={t.trace_id}>
                    {t.trace_id.slice(0, 12)}…（{t.status}）
                  </option>
                ))}
              </select>
              <button
                className="config-button primary"
                onClick={handleStart}
                disabled={starting || !selectedTraceId}
              >
                {starting ? "启动中…" : "启动评估"}
              </button>
            </div>
          </section>

          {(liveLogs.length > 0 || selectedEval || streaming) && (
            <section className="evolve-detail">
              {streaming && (
                <div className="streaming-badge">
                  <span className="pulse" /> 实时流中…
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
              {selectedEval && !streaming && (
                <EvalReport evalSession={selectedEval} onTraceClick={(id) => navigate(`/traces/${id}`)} />
              )}
            </section>
          )}
        </main>
      </div>
    </div>
  );
}

/** 评估报告展示组件。 */
function EvalReport({ evalSession, onTraceClick }: { evalSession: EvalSession; onTraceClick: (id: string) => void }) {
  const scores = evalSession.scores || {};
  const findings = evalSession.findings || [];
  const contentOverall = scores.content_overall as number | undefined;

  return (
    <div className="session-detail">
      <h3>评估报告 {evalSession.eval_id.slice(0, 8)}</h3>
      <div className="detail-grid">
        <div><label>状态</label><span className={`session-status ${evalSession.status}`}>{evalStatusLabel(evalSession.status)}</span></div>
        <div><label>内容总分</label><span className="score-big">{contentOverall != null ? contentOverall.toFixed(2) : "—"}</span></div>
      </div>

      {Object.keys(scores).length > 0 && scores.content_scores && (
        <div className="score-breakdown">
          <h4>内容维度分数</h4>
          <div className="score-bars">
            {Object.entries(scores.content_scores as Record<string, number>).map(([k, v]) => (
              <div key={k} className="score-bar">
                <span className="score-label">{k}</span>
                <div className="score-track">
                  <div className="score-fill" style={{ width: `${(v as number) * 100}%` }} />
                </div>
                <span className="score-num">{(v as number).toFixed(2)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {findings.length > 0 && (
        <div className="findings">
          <h4>问题清单（{findings.length}）</h4>
          {findings.map((f: any, i: number) => (
            <div key={i} className={`finding-item severity-${f.severity}`}>
              <span className="finding-dim">{f.dimension}</span>
              <span className="finding-text">{f.finding}</span>
            </div>
          ))}
        </div>
      )}

      <button className="link-button" onClick={() => onTraceClick(evalSession.trace_id)}>
        查看被评估 trace →
      </button>

      {evalSession.report_md && (
        <div className="report-section">
          <h4>完整报告</h4>
          <div className="prose-doc eval-report-md">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {evalSession.report_md}
            </ReactMarkdown>
          </div>
        </div>
      )}
    </div>
  );
}

function evalStatusLabel(s: string): string {
  const map: Record<string, string> = { running: "运行中", done: "完成", failed: "失败" };
  return map[s] ?? s;
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2_digit", minute: "2-digit" } as any);
  } catch {
    return iso;
  }
}
