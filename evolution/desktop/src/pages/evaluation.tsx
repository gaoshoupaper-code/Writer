import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  getEvalSessions,
  getEvalSession,
  startEval,
  stopEval,
  getTraces,
  getEvalSessionEventsSince,
  startEvidenceCompile,
  getEvidenceSession,
  getCurrentEvidencePack,
  type EvalSession,
  type TraceListItem,
  type EvalFrame,
  type EvidencePack,
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
  const [streamingEvalId, setStreamingEvalId] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const streamCancelRef = useRef<(() => void) | null>(null);
  // 失败态：区分"暂无评估记录"与"加载失败"
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    let esFailed = false;
    let trFailed = false;
    const [es, tr] = await Promise.all([
      getEvalSessions(30).catch(() => { esFailed = true; return null; }),
      getTraces({ limit: 50 }).catch(() => { trFailed = true; return null; }),
    ]);
    if (es !== null) setEvals(es.sessions);
    if (tr !== null) {
      // 排除进化端自观测 trace——评估 Agent 只评估创作 Agent 的 trace，
      // 不能评估自己（evolution_eval）或进化 Agent（evolution_evolve）的录像。
      setTraces(
        tr.items.filter(
          (t) => t.run_purpose !== "evolution_eval" && t.run_purpose !== "evolution_evolve",
        ),
      );
    }

    if (esFailed && trFailed) {
      if (evals.length === 0 && traces.length === 0) {
        setLoadError("评估数据加载失败（evolution 服务不可达或鉴权失败）");
      } else {
        toast.error("评估数据刷新失败，显示的为上次成功拉取的数据");
      }
    } else {
      setLoadError(null);
      if (esFailed || trFailed) {
        toast.error("部分评估数据加载失败，已显示可用数据");
      }
    }
  }, [evals.length, traces.length]);

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
      setStreamingEvalId(resp.eval_id);
      subscribeStream(resp.eval_id);
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "启动评估失败");
    } finally {
      setStarting(false);
    }
  }

  async function handleStop() {
    if (!streamingEvalId) return;
    if (!window.confirm("确定停止评估？")) return;
    setStopping(true);
    try {
      await stopEval(streamingEvalId);
      toast.success("已请求停止评估");
      streamCancelRef.current?.();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "停止失败");
    } finally {
      setStopping(false);
    }
  }

  async function subscribeStream(evalId: string) {
    // trace 稳定性重构（设计 20260720_203000）：从 SSE 改为 Pull 轮询。
    // 评估进行中每 1s 拉一次增量事件帧；eval_status 非 running 时停止。
    if (streamCancelRef.current) streamCancelRef.current();
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let sinceSeq = 0;

    streamCancelRef.current = () => {
      cancelled = true;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    };

    const POLL_INTERVAL_MS = 1000;
    const ERROR_BACKOFF_MS = 3000;

    const poll = async () => {
      if (cancelled) return;
      try {
        const resp = await getEvalSessionEventsSince(evalId, sinceSeq);
        if (cancelled) return;

        if (resp.frames.length > 0) {
          setLiveLogs((prev) => [...prev, ...resp.frames]);
          sinceSeq = resp.max_seq;
        }

        // has_more=true 时立即续拉（事件积压时不等 1s）。
        if (resp.has_more) {
          timer = setTimeout(poll, 0);
          return;
        }

        // eval_status 非 running：评估结束，拉详情 + 停止轮询。
        if (resp.eval_status !== "running") {
          setStreaming(false);
          setStreamingEvalId(null);
          try {
            const detail = await getEvalSession(evalId);
            if (!cancelled) setSelectedEval(detail);
          } catch {
            // 详情拉取失败时退回列表刷新
          }
          if (!cancelled) refresh();
          return;
        }

        // running 中：安排下次轮询。
        timer = setTimeout(poll, POLL_INTERVAL_MS);
      } catch (err) {
        if (cancelled) return;
        // 网络抖动 / 后端临时不可达：退避后继续轮询（不崩，不丢失已拉日志）。
        timer = setTimeout(poll, ERROR_BACKOFF_MS);
      }
    };

    // 立即拉一次（不等 1s）。
    poll();
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
                    setStreamingEvalId(e.eval_id);
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
            {evals.length === 0 && loadError && (
              <div className="page-error">
                <div className="page-error-title">评估记录加载失败</div>
                <div className="page-error-detail">{loadError}</div>
                <button className="page-error-retry" onClick={refresh}>重试</button>
              </div>
            )}
            {evals.length === 0 && !loadError && <div className="monitor-empty">暂无评估记录</div>}
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

  // 证据编译状态（轨迹证据包）
  const [evidenceStatus, setEvidenceStatus] = useState<string>("idle"); // idle|compiling|ready|partial|failed|error
  const [evidencePack, setEvidencePack] = useState<EvidencePack | null>(null);

  async function handleCompileEvidence() {
    setEvidenceStatus("compiling");
    try {
      const resp = await startEvidenceCompile(evalSession.trace_id);
      // 轮询编译状态
      const poll = async () => {
        const pack = await getEvidenceSession(resp.pack_id);
        if (pack.status === "compiling" || pack.status === "pending") {
          setEvidencePack(pack);
          setTimeout(poll, 2000);
        } else {
          setEvidencePack(pack);
          setEvidenceStatus(pack.status);
        }
      };
      poll();
    } catch (e) {
      setEvidenceStatus("error");
    }
  }

  // 页面加载时检查是否已有证据包
  useEffect(() => {
    getCurrentEvidencePack(evalSession.trace_id)
      .then((pack) => {
        setEvidencePack(pack);
        setEvidenceStatus(pack.status);
      })
      .catch(() => {
        // 无证据包是正常状态，不算错误
        setEvidenceStatus("idle");
      });
  }, [evalSession.trace_id]);

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

      {/* 轨迹证据包编译入口（2026-07） */}
      <div className="evidence-compile-section">
        <h4>轨迹证据包</h4>
        {evidenceStatus === "idle" && (
          <button className="config-button primary" onClick={handleCompileEvidence}>
            编译轨迹证据包
          </button>
        )}
        {evidenceStatus === "compiling" && (
          <div className="evidence-status compiling">
            <span className="spinner" /> 编译中… pack_id={evidencePack?.pack_id?.slice(0, 8)}…
          </div>
        )}
        {(evidenceStatus === "ready" || evidenceStatus === "partial") && evidencePack && (
          <div className="evidence-status done">
            <span className={`pack-status ${evidencePack.status}`}>
              {evidencePack.status === "ready" ? "✓ 完整" : "△ 降级"}（v{evidencePack.version}）
            </span>
            {evidencePack.manifest && (
              <div className="evidence-summary">
                <span>完整度: {evidencePack.manifest.completeness}</span>
                <span>适用维度: {evidencePack.manifest.applicable_dimensions?.join(", ")}</span>
                <span>review: {evidencePack.manifest.coverage?.review_calls ?? 0} 次</span>
                <span>错误: {evidencePack.manifest.coverage?.error_events ?? 0} 个</span>
                {evidencePack.manifest.coverage?.gaps?.length > 0 && (
                  <span className="evidence-gaps">⚠ 缺口: {evidencePack.manifest.coverage.gaps.join("; ")}</span>
                )}
              </div>
            )}
            {evidencePack.failure_reason && (
              <div className="evidence-reason">{evidencePack.failure_reason}</div>
            )}
          </div>
        )}
        {evidenceStatus === "failed" && (
          <div className="evidence-status failed">
            ✗ 编译失败：{evidencePack?.failure_reason || "未知原因"}
            <button className="link-button" onClick={handleCompileEvidence}>重试</button>
          </div>
        )}
        {evidenceStatus === "error" && (
          <div className="evidence-status failed">
            ✗ 请求失败
            <button className="link-button" onClick={handleCompileEvidence}>重试</button>
          </div>
        )}
      </div>

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
  const map: Record<string, string> = { running: "运行中", done: "完成", failed: "失败", cancelled: "已停止" };
  return map[s] ?? s;
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2_digit", minute: "2-digit" } as any);
  } catch {
    return iso;
  }
}
