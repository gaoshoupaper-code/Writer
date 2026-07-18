import { useEffect, useState, useCallback, useMemo } from "react";
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
 * 双 Tab（D7）：
 * - 创作监测：run_purpose = user_generation（用户在创作端的创作过程）
 * - 进化监测：run_purpose = evolution_eval + evolution_evolve（进化端自身的评估/进化过程）
 *
 * Tab 只影响活跃 run 列表（前端过滤，S6）；统计概览/skill/失败模式保持全局（D8）。
 */
export default function MonitorPage() {
  const navigate = useNavigate();
  const [overview, setOverview] = useState<StatsOverview | null>(null);
  const [skills, setSkills] = useState<SkillStat[]>([]);
  const [failures, setFailures] = useState<FailurePattern[]>([]);
  const [activeRuns, setActiveRuns] = useState<ActiveRun[]>([]);
  const [loading, setLoading] = useState(true);
  // 全部请求都失败时才显示整页错误态（部分失败走 toast，不阻断展示已拿到的数据）
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"creation" | "evolution">("creation");

  const refresh = useCallback(async () => {
    // 每个请求独立 catch：记录成败而非吞掉，便于区分"真空"与"加载失败"
    let overviewFailed = false;
    const [ov, sk, fl, ar] = await Promise.all([
      getStatsOverview().catch(() => { overviewFailed = true; return null; }),
      getStatsSkills(15).catch(() => null as SkillStat[] | null),
      getStatsFailures(8).catch(() => null as FailurePattern[] | null),
      getActiveRuns().catch(() => null as ActiveRun[] | null),
    ]);
    setOverview(ov);
    if (sk !== null) setSkills(sk);
    if (fl !== null) setFailures(fl);
    if (ar !== null) setActiveRuns(ar);

    // 首次加载（loading=true）全部失败 → 整页错误态；
    // 后续轮询全部失败 → toast 提示但保留旧数据（不刷空）
    const allFailed = ov === null && sk === null && fl === null && ar === null;
    if (allFailed) {
      if (loading) {
        setError("监测数据加载失败（evolution 服务不可达或鉴权失败）");
      } else {
        toast.error("监测数据刷新失败，显示的为上次成功拉取的数据");
      }
    } else {
      setError(null);
      // 部分失败：提示用户某块没更新，但不阻断展示
      if (overviewFailed || sk === null || fl === null || ar === null) {
        toast.error("部分监测数据加载失败，已显示可用数据");
      }
    }
    setLoading(false);
  }, [loading]);

  useEffect(() => {
    refresh();
    // 活跃 run 每 5s 刷新一次
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  // 按 tab 过滤活跃 run（S6：全量拉取 + 前端过滤）
  const filteredRuns = useMemo(() => {
    if (tab === "creation") {
      return activeRuns.filter((r) => !r.run_purpose || r.run_purpose === "user_generation");
    }
    // 进化监测：eval + evolve
    return activeRuns.filter(
      (r) => r.run_purpose === "evolution_eval" || r.run_purpose === "evolution_evolve",
    );
  }, [activeRuns, tab]);

  if (loading) {
    return <div className="page-loading">加载监测数据…</div>;
  }

  if (error) {
    return (
      <div className="page-error">
        <div className="page-error-title">监测数据加载失败</div>
        <div className="page-error-detail">{error}</div>
        <button className="page-error-retry" onClick={() => { setLoading(true); refresh(); }}>
          重试
        </button>
      </div>
    );
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

      {/* 统计概览卡片（全局，不受 tab 影响） */}
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

      {/* 双 Tab */}
      <nav className="inspection-tabs monitor-tabs">
        <button
          className={`inspection-tab ${tab === "creation" ? "active" : ""}`}
          type="button"
          onClick={() => setTab("creation")}
        >
          创作监测
        </button>
        <button
          className={`inspection-tab ${tab === "evolution" ? "active" : ""}`}
          type="button"
          onClick={() => setTab("evolution")}
        >
          进化监测
        </button>
      </nav>

      {/* 活跃 run（按 tab 过滤） */}
      <section className="monitor-section">
        <h2 className="section-title">
          {tab === "creation" ? "创作活跃运行" : "进化活跃运行"}（{filteredRuns.length}）
        </h2>
        {filteredRuns.length === 0 ? (
          <div className="monitor-empty">
            {tab === "creation" ? "当前无创作活跃运行" : "当前无进化活跃运行"}
          </div>
        ) : (
          <div className="run-list">
            {filteredRuns.map((run) => (
              <div
                key={run.trace_id}
                className="run-item"
                onClick={() => navigate(`/traces/${run.trace_id}`)}
              >
                <div className="run-item-main">
                  <span className={`run-status ${run.status}`}>● {run.status}</span>
                  <span className="run-session">{run.session_name || run.endpoint || run.trace_id.slice(0, 12)}</span>
                  {tab === "evolution" && <PurposeBadge purpose={run.run_purpose} />}
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

      {/* skill 统计（全局，不受 tab 影响） */}
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

      {/* 失败模式（全局） */}
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

/** 来源标签（进化端内部区分评估/进化） */
function PurposeBadge({ purpose }: { purpose: string | null }) {
  if (!purpose) return null;
  const label =
    purpose === "evolution_eval" ? "评估"
      : purpose === "evolution_evolve" ? "进化"
      : purpose === "user_generation" ? "执行"
      : purpose;
  const color =
    purpose === "evolution_eval" ? "var(--running, #0f766e)"
      : purpose === "evolution_evolve" ? "#a78bfa"
      : "var(--muted)";
  return (
    <span className="purpose-badge" style={{ color }}>{label}</span>
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
