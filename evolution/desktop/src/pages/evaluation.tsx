import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  getEvalSessions,
  getEvalSession,
  startEval,
  stopEval,
  getEvalSessionEventsSince,
  getConsumableDossiers,
  type EvalSession,
  type EvalFrame,
  type ConsumableDossierSummary,
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
  const [consumableDossiers, setConsumableDossiers] = useState<ConsumableDossierSummary[]>([]);
  const [selectedEval, setSelectedEval] = useState<EvalSession | null>(null);
  const [selectedDossierId, setSelectedDossierId] = useState("");
  const [starting, setStarting] = useState(false);
  const [liveLogs, setLiveLogs] = useState<any[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamingEvalId, setStreamingEvalId] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const streamCancelRef = useRef<(() => void) | null>(null);
  // 失败态：区分"暂无评估记录"与"加载失败"
  const [loadError, setLoadError] = useState<string | null>(null);
  // 从证据卷宗页跳转预选（?dossier=xxx）
  const [searchParams] = useSearchParams();

  const refresh = useCallback(async () => {
    let esFailed = false;
    let dosFailed = false;
    const [es, dos] = await Promise.all([
      getEvalSessions(30).catch(() => { esFailed = true; return null; }),
      getConsumableDossiers(200).catch(() => { dosFailed = true; return null; }),
    ]);
    if (es !== null) setEvals(es.sessions);
    if (dos !== null) setConsumableDossiers(dos.dossiers);

    if (esFailed && dosFailed) {
      if (evals.length === 0 && consumableDossiers.length === 0) {
        setLoadError("评估数据加载失败（evolution 服务不可达或鉴权失败）");
      } else {
        toast.error("评估数据刷新失败，显示的为上次成功拉取的数据");
      }
    } else {
      setLoadError(null);
      if (esFailed || dosFailed) {
        toast.error("部分评估数据加载失败，已显示可用数据");
      }
    }
  }, [evals.length, consumableDossiers.length]);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 10000);
    return () => {
      clearInterval(timer);
      if (streamCancelRef.current) streamCancelRef.current();
    };
  }, [refresh]);

  // 从证据卷宗页跳转预选 dossier（?dossier=xxx）
  useEffect(() => {
    const preselected = searchParams.get("dossier");
    if (preselected && !selectedDossierId) {
      setSelectedDossierId(preselected);
    }
  }, [searchParams, selectedDossierId]);

  async function handleStart() {
    if (!selectedDossierId) {
      toast.error("请先选择一个证据卷宗（须在证据卷宗模块编译为完整态）");
      return;
    }
    setStarting(true);
    setLiveLogs([]);
    try {
      const resp = await startEval(selectedDossierId);
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
            <div className="evolve-start-hint">
              评估只接受完整证据卷宗（ready）。请先在
              <a onClick={() => navigate("/dossiers")}>证据卷宗模块</a>
              编译。
            </div>
            <div className="evolve-start-form">
              <select
                className="evolve-select"
                value={selectedDossierId}
                onChange={(e) => setSelectedDossierId(e.target.value)}
                disabled={starting}
              >
                <option value="">选择证据卷宗…</option>
                {consumableDossiers.map((d) => (
                  <option key={d.dossier_id} value={d.dossier_id}>
                    trace {d.trace_id.slice(0, 10)}… · v{d.version} · {d.completeness || "?"}
                  </option>
                ))}
              </select>
              <button
                className="config-button primary"
                onClick={handleStart}
                disabled={starting || !selectedDossierId}
              >
                {starting ? "启动中…" : "启动评估"}
              </button>
            </div>
            {consumableDossiers.length === 0 && (
              <div className="muted">暂无完整证据卷宗。先去证据卷宗模块编译。</div>
            )}
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
                <EvalReport
                  evalSession={selectedEval}
                  onTraceClick={(id) => navigate(`/traces/${id}`)}
                  onNavigateDossiers={() => navigate("/dossiers")}
                />
              )}
            </section>
          )}
        </main>
      </div>
    </div>
  );
}

/** 评估报告展示组件。 */
function EvalReport({ evalSession, onTraceClick, onNavigateDossiers }: { evalSession: EvalSession; onTraceClick: (id: string) => void; onNavigateDossiers: () => void }) {
  const scores = evalSession.scores || {};
  const findings = evalSession.findings || [];

  return (
    <div className="session-detail">
      <h3>评估报告 {evalSession.eval_id.slice(0, 8)}</h3>
      <div className="detail-grid">
        <div><label>状态</label><span className={`session-status ${evalSession.status}`}>{evalStatusLabel(evalSession.status)}</span></div>
        <div><label>校准状态</label><span className={`calibration-status ${scores.calibration || "unknown"}`}>{scores.calibration === "uncalibrated" ? "⚠ 未校准" : scores.calibration || "—"}</span></div>
      </div>

      {/* 28 维五级锚点按组展示（第三期） */}
      {scores.groups && Object.keys(scores.groups).length > 0 && (
        <div className="score-breakdown">
          {Object.entries(scores.groups as Record<string, any>).map(([groupName, groupData]: [string, any]) => {
            if (groupData.skipped) return null;
            const groupScores = groupData.scores || {};
            const validScores = Object.entries(groupScores).filter(([, v]: [string, any]) => v > 0) as [string, number][];
            const groupAvg = validScores.length > 0
              ? (validScores.reduce((sum: number, [, v]: [string, number]) => sum + v, 0) / validScores.length).toFixed(1)
              : "—";
            const groupLabels: Record<string, string> = {
              general: "通用五维", storybuilding: "玄幻故事构建",
              outline: "玄幻详细大纲", body: "玄幻正文",
            };
            return (
              <div key={groupName} className="score-group">
                <h4>{groupLabels[groupName] || groupName} <span className="group-avg">均分 {groupAvg}</span> <span className="dim-count">({validScores.length}/{groupData.total_dim_count || validScores.length}维)</span></h4>
                <div className="score-bars">
                  {Object.entries(groupScores).map(([k, v]: [string, any]) => (
                    <div key={k} className={`score-bar ${v === 0 ? "no-data" : ""} ${v > 0 && v < 3 ? "low-score" : ""}`}>
                      <span className="score-label">{k}</span>
                      <div className="score-track">
                        <div className="score-fill" style={{ width: `${v > 0 ? (v / 5) * 100 : 0}%` }} />
                      </div>
                      <span className="score-num">{v > 0 ? `${v}/5` : "N/A"}</span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
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

      {/* 阶段 E：评估基于证据卷宗，展示绑定的卷宗信息 */}
      <div className="evidence-compile-section">
        <h4>证据卷宗</h4>
        {evalSession.bound_dossier_id ? (
          <div className="evidence-status done">
            <span>绑定卷宗：{evalSession.bound_dossier_id.slice(0, 12)}…</span>
            {evalSession.sealed_dossier_id && (
              <span className="pack-status ready">✓ 已封存评估卷宗 {evalSession.sealed_dossier_id.slice(0, 12)}…</span>
            )}
            <button className="link-button" onClick={onNavigateDossiers}>
              卷宗管理 →
            </button>
          </div>
        ) : (
          <div className="muted">旧链路评估（未绑定证据卷宗）</div>
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
