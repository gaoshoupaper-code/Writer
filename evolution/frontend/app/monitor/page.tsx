"use client";

/**
 * 进化端自观测监测面板（D12 轻量版）。
 *
 * 展示进化端自己的 agent（评估/进化）执行情况：
 *   - 活跃 trace（正在跑的评估/进化，实时刷新）
 *   - 历史进化端 trace 列表（run_purpose=evolution_eval/evolution_evolve）
 *
 * 与现有 /traces 页（执行端 trace 追溯）分离——这是进化端"自我监测"的独立入口。
 * 完整统计（token 成本趋势/错误率）后续迭代加。
 */
import { useCallback, useEffect, useState } from "react";
import { fetchTraces, fetchActiveRuns } from "@/lib/monitor-api";
import { StatusBadge } from "@/components/StatusBadge";
import type { ActiveRun, TraceListItem } from "@/lib/types";

const REFRESH_INTERVAL = 5000; // 活跃大盘 5s 刷新

export default function MonitorPage() {
  const [active, setActive] = useState<ActiveRun[]>([]);
  const [traces, setTraces] = useState<TraceListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [activeRuns, evalTraces, evolveTraces] = await Promise.all([
        fetchActiveRuns(),
        fetchTraces({ runPurpose: "evolution_eval", limit: 100 }),
        fetchTraces({ runPurpose: "evolution_evolve", limit: 100 }),
      ]);
      setActive(activeRuns);
      // 合并评估+进化 trace，按时间倒序
      const merged = [...evalTraces, ...evolveTraces].sort(
        (a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? "")
      );
      setTraces(merged);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_INTERVAL);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <div>
      <h1 className="page-title">进化端监测</h1>
      <p className="page-subtitle">
        评估 Agent 与进化 Agent 的自观测 · 活跃 {active.length} 条 / 历史 {traces.length} 条
      </p>

      {error && <div className="error-box">{error}</div>}

      {/* 活跃 trace */}
      <div className="card" style={{ padding: 16, marginBottom: 20 }}>
        <h3 style={{ marginBottom: 12 }}>活跃 trace（实时）</h3>
        {active.length === 0 ? (
          <div className="text-dim" style={{ padding: 16, textAlign: "center" }}>
            当前无活跃 trace
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Trace</th>
                <th>来源</th>
                <th>耗时</th>
                <th>事件数</th>
              </tr>
            </thead>
            <tbody>
              {active.map((r) => (
                <tr
                  key={r.trace_id}
                  onClick={() => (window.location.href = `/traces/?id=${r.trace_id}`)}
                  style={{ cursor: "pointer" }}
                >
                  <td className="mono">{r.trace_id.slice(0, 18)}</td>
                  <td>
                    <PurposeBadge purpose={(r as Record<string, unknown>).run_purpose as string} />
                  </td>
                  <td className="mono text-dim">{formatDuration(r.duration_ms)}</td>
                  <td className="mono text-dim">{r.event_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 历史 trace 列表 */}
      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Trace</th>
              <th>状态</th>
              <th>来源</th>
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
                  暂无进化端 trace（启动一次评估或进化后这里会出现）
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
                  <td><PurposeBadge purpose={t.run_purpose} /></td>
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

function PurposeBadge({ purpose }: { purpose?: string }) {
  const label =
    purpose === "evolution_eval" ? "评估"
      : purpose === "evolution_evolve" ? "进化"
      : purpose === "user_generation" ? "执行"
      : purpose ?? "—";
  const color =
    purpose === "evolution_eval" ? "var(--running)"
      : purpose === "evolution_evolve" ? "#a78bfa"
      : "var(--text-dim)";
  return (
    <span style={{ fontSize: 12, color, fontWeight: 500 }}>{label}</span>
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
