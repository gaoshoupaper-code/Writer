import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  getStatsOverview,
  getStatsSkills,
  getStatsFailures,
  getActiveRuns,
  type StatsOverview,
  type SkillStat,
  type FailurePattern,
  type ActiveRun,
} from "@/lib/api";

/**
 * 监测大盘（设计文档信息架构：默认首屏）。
 *
 * 四个区块：
 * 1. 统计概览卡片（总数/成功率/token/延迟分位）
 * 2. 活跃 run 列表（实时刷新，点进下钻 trace）
 * 3. skill 调用统计（哪个 agent 跑得多/失败多）
 * 4. 失败模式聚类（常见 error）
 */
export default function MonitorPage() {
  const navigate = useNavigate();
  const [overview, setOverview] = useState<StatsOverview | null>(null);
  const [skills, setSkills] = useState<SkillStat[]>([]);
  const [failures, setFailures] = useState<FailurePattern[]>([]);
  const [activeRuns, setActiveRuns] = useState<ActiveRun[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [ov, sk, fl, ar] = await Promise.all([
        getStatsOverview().catch(() => null),
        getStatsSkills(15).catch(() => []),
        getStatsFailures(8).catch(() => []),
        getActiveRuns().catch(() => []),
      ]);
      setOverview(ov);
      setSkills(sk);
      setFailures(fl);
      setActiveRuns(ar);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取监测数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    // 活跃 run 每 5s 刷新一次
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  if (loading) {
    return <div className="page-loading">加载监测数据…</div>;
  }

  const successRate = overview && overview.total > 0
    ? ((overview.success / overview.total) * 100).toFixed(1)
    : "—";

  return (
    <div className="monitor-page">
      <header className="page-header">
        <h1>监测大盘</h1>
        <p className="page-desc">总览运行健康度、活跃任务与失败模式（每 5 秒自动刷新）</p>
      </header>

      {/* 统计概览卡片 */}
      <section className="stats-grid">
        <div className="stat-card">
          <span className="stat-value">{overview?.total ?? 0}</span>
          <span className="stat-label">总运行数</span>
          <span className="stat-sub">成功 {overview?.success ?? 0} / 失败 {overview?.failed ?? 0}</span>
        </div>
        <div className="stat-card">
          <span className="stat-value">{successRate}%</span>
          <span className="stat-label">成功率</span>
          <span className="stat-sub">错误率 {((overview?.error_rate ?? 0) * 100).toFixed(1)}%</span>
        </div>
        <div className="stat-card">
          <span className="stat-value">{formatTokens(overview?.total_tokens ?? 0)}</span>
          <span className="stat-label">总 Token</span>
          <span className="stat-sub">入 {formatTokens(overview?.total_input_tokens ?? 0)} / 出 {formatTokens(overview?.total_output_tokens ?? 0)}</span>
        </div>
        <div className="stat-card">
          <span className="stat-value">{formatMs(overview?.duration_p50)}</span>
          <span className="stat-label">P50 延迟</span>
          <span className="stat-sub">P90 {formatMs(overview?.duration_p90)} / P99 {formatMs(overview?.duration_p99)}</span>
        </div>
      </section>

      {/* 活跃 run */}
      <section className="monitor-section">
        <h2 className="section-title">活跃运行（{activeRuns.length}）</h2>
        {activeRuns.length === 0 ? (
          <div className="monitor-empty">当前无活跃运行</div>
        ) : (
          <div className="run-list">
            {activeRuns.map((run) => (
              <div
                key={run.trace_id}
                className="run-item"
                onClick={() => navigate(`/traces/${run.trace_id}`)}
              >
                <div className="run-item-main">
                  <span className={`run-status ${run.status}`}>● {run.status}</span>
                  <span className="run-session">{run.session_name || run.endpoint || run.trace_id.slice(0, 12)}</span>
                </div>
                <div className="run-item-meta">
                  <span>{run.event_count} 事件</span>
                  <span>{formatMs(run.duration_ms)}</span>
                  <span className={run.ingested ? "ingested" : "not-ingested"}>
                    {run.ingested ? "已入库" : "采集中"}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* skill 统计 */}
      <section className="monitor-section">
        <h2 className="section-title">Agent 调用统计</h2>
        {skills.length === 0 ? (
          <div className="monitor-empty">暂无 skill 统计数据</div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Agent</th>
                <th>调用次数</th>
                <th>失败率</th>
                <th>平均耗时</th>
              </tr>
            </thead>
            <tbody>
              {skills.map((s) => (
                <tr key={s.agent_name}>
                  <td>{s.agent_name}</td>
                  <td>{s.call_count}</td>
                  <td className={s.fail_rate > 0.1 ? "cell-warn" : ""}>
                    {(s.fail_rate * 100).toFixed(1)}%
                  </td>
                  <td>{formatMs(s.avg_duration_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* 失败模式 */}
      {failures.length > 0 && (
        <section className="monitor-section">
          <h2 className="section-title">常见失败模式</h2>
          <div className="failure-list">
            {failures.map((f, i) => (
              <div key={i} className="failure-item">
                <span className="failure-count">{f.count}×</span>
                <code className="failure-pattern" title={f.error_pattern}>{f.error_pattern}</code>
                {f.sample_trace_ids[0] && (
                  <button
                    className="failure-link"
                    onClick={() => navigate(`/traces/${f.sample_trace_ids[0]}`)}
                  >
                    查看 →
                  </button>
                )}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}
