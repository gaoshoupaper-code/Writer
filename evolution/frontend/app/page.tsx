"use client";

/**
 * 进化总览（首页，替换旧 adapt 驾驶舱入口）。
 *
 * 单进化 Agent 视角：
 *   1. 启动进化入口（选 case → POST /evolve/start → 跳转 session 页）
 *   2. session 列表：最近 N 次进化，状态/分数/是否改进
 *
 * 叙事：这不是多轮自动进化循环，是"手动触发一次进化 Agent 自主分析+改+验证"。
 */
import { useCallback, useEffect, useState } from "react";
import { fetchSessions, fetchCases, startEvolve } from "@/lib/evolve-api";
import type { EvolveSession, CaseSummary } from "@/lib/evolve-api";

export default function EvolutionOverviewPage() {
  const [sessions, setSessions] = useState<EvolveSession[]>([]);
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showStart, setShowStart] = useState(false);
  const [selectedCase, setSelectedCase] = useState("case-001");
  const [starting, setStarting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [sess, cs] = await Promise.all([fetchSessions(), fetchCases()]);
      setSessions(sess);
      setCases(cs.length > 0 ? cs : [{ case_id: "case-001", title: "case-001" }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleStart = async () => {
    setStarting(true);
    setError(null);
    try {
      const { session_id } = await startEvolve(selectedCase);
      window.location.href = `/sessions/?id=${session_id}`;
    } catch (e) {
      setError(e instanceof Error ? e.message : "启动失败");
      setStarting(false);
    }
  };

  const doneCount = sessions.filter((s) => s.status === "done").length;
  const improvedCount = sessions.filter((s) => {
    if (s.baseline_score == null || s.candidate_score == null) return false;
    return s.candidate_score > s.baseline_score;
  }).length;

  return (
    <div>
      <div className="overview-head">
        <div>
          <h1 className="page-title">进化总览</h1>
          <p className="page-subtitle">
            单进化 Agent · 自主分析 + 改进 + 验证
          </p>
        </div>
        <button className="btn-primary" onClick={() => setShowStart(true)}>
          启动进化
        </button>
      </div>

      {error && <div className="error-box">{error}</div>}

      {/* 状态条 */}
      <div className="stat-row">
        <StatBlock label="累计 session" value={String(sessions.length)} />
        <StatBlock label="完成" value={String(doneCount)} />
        <StatBlock label="改进成功" value={String(improvedCount)} accent />
        <StatBlock label="评估集" value={String(cases.length)} />
      </div>

      {/* session 列表 */}
      <section style={{ marginTop: 28 }}>
        <div className="section-head">
          <h2 className="section-title">进化 session</h2>
          <span className="text-mute mono" style={{ fontSize: 11 }}>
            最近 {sessions.length} 次进化
          </span>
        </div>
        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>session</th>
                <th>case</th>
                <th>状态</th>
                <th>baseline</th>
                <th>candidate</th>
                <th>结果</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={7} className="text-mute" style={{ textAlign: "center", padding: 32 }}>
                    加载中…
                  </td>
                </tr>
              ) : sessions.length === 0 ? (
                <tr>
                  <td colSpan={7} className="text-dim" style={{ textAlign: "center", padding: 32 }}>
                    还没有进化记录。点击右上「启动进化」开始第一次。
                  </td>
                </tr>
              ) : (
                sessions.map((s) => {
                  const improved =
                    s.baseline_score != null &&
                    s.candidate_score != null &&
                    s.candidate_score > s.baseline_score;
                  return (
                    <tr
                      key={s.session_id}
                      onClick={() => (window.location.href = `/sessions/?id=${s.session_id}`)}
                      style={{ cursor: "pointer" }}
                    >
                      <td className="mono">{s.session_id}</td>
                      <td className="mono text-dim">{s.case_id}</td>
                      <td>
                        <StatusMini status={s.status} />
                      </td>
                      <td className="mono">
                        {s.baseline_score != null ? s.baseline_score.toFixed(3) : "—"}
                      </td>
                      <td className="mono">
                        {s.candidate_score != null ? s.candidate_score.toFixed(3) : "—"}
                      </td>
                      <td>
                        {s.baseline_score != null && s.candidate_score != null ? (
                          <span
                            className="status-badge"
                            style={{
                              color: improved ? "var(--completed)" : "var(--cancelled)",
                              background: improved ? "rgba(63,185,80,.1)" : "rgba(139,148,158,.1)",
                            }}
                          >
                            {improved ? "↑改进" : "↓未改"}
                          </span>
                        ) : (
                          <span className="text-mute">—</span>
                        )}
                      </td>
                      <td className="mono text-mute">{fmtTime(s.created_at)}</td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* 启动进化对话框 */}
      {showStart && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,.5)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 50,
          }}
          onClick={() => !starting && setShowStart(false)}
        >
          <div
            className="card"
            style={{ padding: 24, minWidth: 360, maxWidth: "90vw" }}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 style={{ marginTop: 0 }}>启动一次进化</h3>
            <p className="text-dim" style={{ fontSize: 13, marginBottom: 16 }}>
              选择评估集 case，进化 Agent 会自主跑完整闭环：
              跑 baseline → 分析 → 改进 → 重跑 → 比分 → 报告。
            </p>
            <label className="text-mute mono" style={{ fontSize: 11, display: "block", marginBottom: 6 }}>
              评估集 case
            </label>
            <select
              value={selectedCase}
              onChange={(e) => setSelectedCase(e.target.value)}
              disabled={starting}
              style={{
                width: "100%",
                padding: "8px 12px",
                background: "var(--bg)",
                color: "var(--text)",
                border: "1px solid var(--border)",
                borderRadius: 4,
                marginBottom: 16,
              }}
            >
              {cases.map((c) => (
                <option key={c.case_id} value={c.case_id}>
                  {c.title}
                </option>
              ))}
            </select>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button
                className="btn-ghost"
                onClick={() => setShowStart(false)}
                disabled={starting}
              >
                取消
              </button>
              <button className="btn-primary" onClick={handleStart} disabled={starting}>
                {starting ? "启动中…" : "启动"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function StatBlock({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div className="stat-block">
      <div className="stat-label">{label}</div>
      <div
        className="stat-value mono"
        style={accent ? { color: "var(--accent)" } : undefined}
      >
        {value}
      </div>
    </div>
  );
}

function StatusMini({ status }: { status: string }) {
  const map: Record<string, { label: string; color: string }> = {
    running: { label: "运行中", color: "var(--accent)" },
    done: { label: "完成", color: "var(--completed)" },
    failed: { label: "失败", color: "var(--cancelled)" },
  };
  const cfg = map[status] || { label: status, color: "var(--text-dim)" };
  return (
    <span
      className="status-badge mono"
      style={{ color: cfg.color, border: `1px solid ${cfg.color}40` }}
    >
      {cfg.label}
    </span>
  );
}

function fmtTime(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n: number) => String(n).padStart(2, "0");
  const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (sameDay) return hhmm;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hhmm}`;
}
