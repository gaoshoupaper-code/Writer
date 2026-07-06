"use client";

/**
 * Trace 页（/traces）—— 列表 + 详情合一（query 参数切换，适配 output:export）。
 *
 * 无 id 参数：列表视图（D5 精简保留，追溯 baseline/候选 trace）
 * 有 id 参数：详情视图（复用 TracePanel 富交互 + SSE）
 */
import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { fetchTraces } from "@/lib/monitor-api";
import { useTraceStream } from "@/hooks/useTraceStream";
import { TracePanel } from "@/components/trace/TracePanel";
import { StatusBadge } from "@/components/StatusBadge";
import type { TraceListItem, TraceRunSummary } from "@/lib/types";

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "全部状态" },
  { value: "running", label: "运行中" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "失败" },
];

const PURPOSE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "全部来源" },
  { value: "user_generation", label: "执行端" },
  { value: "evolution_eval", label: "进化端·评估" },
  { value: "evolution_evolve", label: "进化端·进化" },
];

export default function TracePage() {
  return (
    <Suspense fallback={<div className="text-dim" style={{ padding: 48 }}>加载中…</div>}>
      <TraceInner />
    </Suspense>
  );
}

function TraceInner() {
  const searchParams = useSearchParams();
  const traceId = searchParams.get("id") ?? "";

  if (traceId) return <TraceDetail traceId={traceId} />;
  return <TraceList />;
}

// ── 列表 ───────────────────────────────────────────────────

function TraceList() {
  const [traces, setTraces] = useState<TraceListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [purposeFilter, setPurposeFilter] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchTraces({
        status: statusFilter || undefined,
        runPurpose: purposeFilter || undefined,
        limit: 200,
      });
      setTraces(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [statusFilter, purposeFilter]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div>
      <h1 className="page-title">Trace 追溯</h1>
      <p className="page-subtitle">
        baseline 与候选 trace 的执行链路 · 共 {traces.length} 条
      </p>

      {error && <div className="error-box">{error}</div>}

      <div className="filter-bar">
        <select
          className="select-input"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <select
          className="select-input"
          value={purposeFilter}
          onChange={(e) => setPurposeFilter(e.target.value)}
        >
          {PURPOSE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <button className="btn-ghost" onClick={load} style={{ marginLeft: "auto" }}>
          刷新
        </button>
      </div>

      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Trace</th>
              <th>状态</th>
              <th>会话</th>
              <th>耗时</th>
              <th>事件</th>
              <th>开始时间</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={6} className="text-mute" style={{ textAlign: "center", padding: 32 }}>
                  加载中…
                </td>
              </tr>
            ) : traces.length === 0 ? (
              <tr>
                <td colSpan={6} className="text-dim" style={{ textAlign: "center", padding: 32 }}>
                  无符合条件的 trace
                </td>
              </tr>
            ) : (
              traces.map((t) => (
                <tr
                  key={t.trace_id}
                  onClick={() => (window.location.href = `/traces/?id=${t.trace_id}`)}
                  style={{ cursor: "pointer" }}
                >
                  <td className="mono">{t.trace_id.slice(0, 18)}</td>
                  <td><StatusBadge status={t.status} /></td>
                  <td className="text-dim">{t.session_name ?? "—"}</td>
                  <td className="mono text-dim">{formatDuration(t.duration_ms)}</td>
                  <td className="mono text-dim">{t.event_count}</td>
                  <td className="mono text-mute">{fmtTime(t.started_at)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── 详情 ───────────────────────────────────────────────────

function TraceDetail({ traceId }: { traceId: string }) {
  const { detail, isLive, loading, error } = useTraceStream(traceId);
  const run: TraceRunSummary | null = detail?.run ?? null;

  return (
    <div>
      <a href="/traces/" className="cockpit-back mono text-mute">← 列表</a>

      <div
        className="card"
        style={{
          display: "flex",
          gap: 28,
          alignItems: "center",
          flexWrap: "wrap",
          margin: "12px 0 16px",
          padding: "14px 18px",
        }}
      >
        <MetaField label="Trace ID">
          <span className="mono" style={{ fontSize: 13 }}>{traceId.slice(0, 20)}</span>
        </MetaField>
        <MetaField label="状态">
          {run ? <StatusBadge status={run.status} /> : <span className="text-dim">—</span>}
        </MetaField>
        <MetaField label="耗时">
          <span className="mono">{run?.duration_ms != null ? formatDuration(run.duration_ms) : "—"}</span>
        </MetaField>
        <MetaField label="事件数">
          <span className="mono">{run?.event_count ?? "—"}</span>
        </MetaField>
        <MetaField label="实时">
          {isLive ? (
            <span style={{ color: "var(--running)", fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span className="pulse-dot" style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--running)" }} />
              实时同步
            </span>
          ) : (
            <span className="text-dim" style={{ fontSize: 12 }}>历史快照</span>
          )}
        </MetaField>
      </div>

      {error && !detail ? (
        <div className="error-box">{error}</div>
      ) : loading ? (
        <div className="card">
          <div className="empty-state">
            <h3>加载中</h3>
            <p>正在拉取 trace 详情</p>
          </div>
        </div>
      ) : !detail ? (
        <div className="card">
          <div className="empty-state">
            <h3>无 trace 数据</h3>
            <p>该 trace 不存在或尚未被摄入</p>
          </div>
        </div>
      ) : (
        <TracePanel
          runs={[detail.run]}
          detail={detail}
          activeTraceId={traceId}
          loading={loading}
          hasActiveThread={true}
          deletingTraceId={""}
          onSelectTrace={() => {}}
          onDeleteTrace={() => {}}
        />
      )}
    </div>
  );
}

function MetaField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="field-label" style={{ marginBottom: 4 }}>{label}</div>
      {children}
    </div>
  );
}

function formatDuration(ms: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  if (ms < 3600000) return `${(ms / 60000).toFixed(1)}min`;
  return `${(ms / 3600000).toFixed(1)}h`;
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
