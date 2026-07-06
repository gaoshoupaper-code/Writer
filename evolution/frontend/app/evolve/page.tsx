"use client";

/**
 * 进化系统（/evolve）—— 功能③ 进化 Agent（三功能解耦，决策 S3/S8）。
 *
 * 精简为「方案→执行」两阶段，强前置需选已评估的 trace：
 *   1. 启动进化入口（选已评估 trace → POST /evolve/start → 跳 session 详情）
 *   2. session 列表：最近 N 次进化，状态（running/pending_review/published/discarded）
 *
 * ?trace=xxx（从评估详情页跳来）→ 自动选中该 trace。
 */
import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { fetchSessions, fetchEvaluatedTraces, startEvolve } from "@/lib/evolve-api";
import type { EvolveSession, EvalSession } from "@/lib/evolve-api";

const STATUS_LABELS: Record<string, string> = {
  running: "执行中",
  pending_review: "待审",
  published: "已发版",
  discarded: "已丢弃",
  failed: "失败",
};

const STATUS_COLORS: Record<string, string> = {
  running: "var(--warn)",
  pending_review: "var(--accent)",
  published: "var(--accent)",
  discarded: "var(--text-dim)",
  failed: "var(--danger)",
};

export default function EvolutionPage() {
  return (
    <Suspense
      fallback={
        <div className="text-dim" style={{ padding: 48 }}>
          加载中…
        </div>
      }
    >
      <EvolveInner />
    </Suspense>
  );
}

function EvolveInner() {
  const params = useSearchParams();
  const presetTrace = params.get("trace") || "";

  const [sessions, setSessions] = useState<EvolveSession[]>([]);
  const [evaluatedTraces, setEvaluatedTraces] = useState<EvalSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showStart, setShowStart] = useState(false);
  const [selectedTraceId, setSelectedTraceId] = useState("");
  const [starting, setStarting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [sess, evals] = await Promise.all([fetchSessions(), fetchEvaluatedTraces()]);
      setSessions(sess);
      setEvaluatedTraces(evals);
      // 从评估页跳来时预选 trace
      if (presetTrace) {
        setSelectedTraceId(presetTrace);
        setShowStart(true);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [presetTrace]);

  useEffect(() => {
    load();
  }, [load]);

  const handleStart = async () => {
    if (!selectedTraceId) return;
    setStarting(true);
    setError(null);
    try {
      const resp = await startEvolve(selectedTraceId);
      window.location.href = `/sessions?id=${encodeURIComponent(resp.session_id)}`;
    } catch (e) {
      setError(e instanceof Error ? e.message : "启动进化失败");
    } finally {
      setStarting(false);
    }
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 16, marginBottom: 24 }}>
        <h1 style={{ margin: 0 }}>进化系统</h1>
        <span className="text-dim">
          进化 Agent · 吃评估报告产改动 · 方案→执行两阶段 · 人工发版
        </span>
        <div style={{ flex: 1 }} />
        <button
          className="btn-primary"
          onClick={() => setShowStart(true)}
        >
          启动进化
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {/* working 区锁定提示（有待审 session 时） */}
      {sessions.some((s) => s.status === "pending_review") && (
        <div className="warn-banner">
          ⚠ 当前有待审改动未处理（发版或丢弃后才能启动新进化）
        </div>
      )}

      {showStart && (
        <div className="modal-overlay" onClick={() => !starting && setShowStart(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 640 }}>
            <h3 style={{ marginTop: 0 }}>启动进化</h3>
            <p className="text-dim" style={{ fontSize: 13 }}>
              选择一条<span style={{ color: "var(--accent)" }}>已评估</span>的 trace，进化 Agent 将基于其评估报告产出改进。
            </p>
            <label className="field-label">已评估的 trace（强前置）</label>
            {evaluatedTraces.length === 0 ? (
              <div className="text-dim" style={{ padding: 16 }}>
                没有已评估的 trace。请先去「手动测试」跑 trace → 再去「评估」评估。
              </div>
            ) : (
              <select
                className="field-select"
                value={selectedTraceId}
                onChange={(e) => setSelectedTraceId(e.target.value)}
                size={8}
                style={{ width: "100%" }}
              >
                {evaluatedTraces.map((e) => (
                  <option key={e.eval_id} value={e.trace_id}>
                    {e.trace_id} · {(e.findings || []).length} 条诊断 · {e.created_at.slice(0, 10)}
                  </option>
                ))}
              </select>
            )}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
              <button className="btn-ghost" onClick={() => setShowStart(false)} disabled={starting}>
                取消
              </button>
              <button
                className="btn-primary"
                onClick={handleStart}
                disabled={starting || !selectedTraceId || evaluatedTraces.length === 0}
              >
                {starting ? "启动中…" : "开始进化"}
              </button>
            </div>
          </div>
        </div>
      )}

      {loading ? (
        <div className="text-dim">加载中…</div>
      ) : sessions.length === 0 ? (
        <div className="empty-state">
          <div className="text-dim">还没有进化记录</div>
          <div className="text-dim" style={{ fontSize: 13, marginTop: 8 }}>
            先去评估一条 trace，再回来启动进化
          </div>
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>session</th>
              <th>trace</th>
              <th>评估</th>
              <th>状态</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <tr
                key={s.session_id}
                onClick={() => (window.location.href = `/sessions?id=${encodeURIComponent(s.session_id)}`)}
                style={{ cursor: "pointer" }}
              >
                <td className="mono">{s.session_id}</td>
                <td className="mono">{(s.baseline_trace || s.trace_id || "-").slice(0, 16)}</td>
                <td className="mono text-dim">{s.eval_ref || "-"}</td>
                <td>
                  <span style={{ color: STATUS_COLORS[s.status] || "var(--text-dim)" }}>
                    {STATUS_LABELS[s.status] || s.status}
                  </span>
                </td>
                <td className="text-dim">{s.created_at.slice(0, 19).replace("T", " ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
