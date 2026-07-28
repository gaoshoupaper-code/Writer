import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  getActiveRuns,
  getTraces,
  type ActiveRun,
  type TraceListItem,
} from "@/lib/api";
import type { TraceIntegrityStatus, TraceWorkload } from "@/lib/types";

const PAGE_SIZE = 50;
const WORKLOADS: Array<{ value: TraceWorkload | "all"; label: string }> = [
  { value: "all", label: "全部工作负载" },
  { value: "creation", label: "创作" },
  { value: "evidence_compile", label: "证据编译" },
  { value: "evaluation", label: "评估" },
  { value: "evolution", label: "进化" },
];
const INTEGRITIES: Array<{ value: TraceIntegrityStatus | "all"; label: string }> = [
  { value: "all", label: "全部完整性" },
  { value: "verified", label: "已验证" },
  { value: "incomplete", label: "不完整" },
  { value: "conflict", label: "冲突" },
  { value: "legacy", label: "旧版" },
];
type TimeRange = "24h" | "7d" | "30d" | "all";

export default function MonitorPage() {
  const navigate = useNavigate();
  const [workload, setWorkload] = useState<TraceWorkload | "all">("all");
  const [integrity, setIntegrity] = useState<TraceIntegrityStatus | "all">("all");
  const [timeRange, setTimeRange] = useState<TimeRange>("7d");
  const [page, setPage] = useState(0);
  const [activeRuns, setActiveRuns] = useState<ActiveRun[]>([]);
  const [recentRuns, setRecentRuns] = useState<TraceListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const since = useMemo(() => {
    if (timeRange === "all") return undefined;
    const hours = timeRange === "24h" ? 24 : timeRange === "7d" ? 24 * 7 : 24 * 30;
    return new Date(Date.now() - hours * 60 * 60 * 1000).toISOString();
  }, [timeRange]);

  const refresh = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const [active, recent] = await Promise.all([
        getActiveRuns(),
        getTraces({
          workload: workload === "all" ? undefined : workload,
          integrity_status: integrity === "all" ? undefined : integrity,
          since,
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
        }),
      ]);
      setActiveRuns(active);
      setRecentRuns(recent.items);
      setTotal(recent.total);
      setError(null);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "运行观测数据加载失败";
      if (silent) toast.error(message);
      else setError(message);
    } finally {
      if (!silent) setLoading(false);
    }
  }, [integrity, page, since, workload]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => refresh(true), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const filteredActive = useMemo(
    () => activeRuns.filter((run) =>
      (workload === "all" || run.workload === workload)
      && (integrity === "all" || run.integrity_status === integrity)),
    [activeRuns, integrity, workload],
  );
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  function resetFilters(next: () => void) {
    next();
    setPage(0);
  }

  if (loading) return <div className="page-loading">加载运行观测…</div>;
  if (error) {
    return (
      <div className="page-error">
        <div className="page-error-title">运行观测加载失败</div>
        <div className="page-error-detail">{error}</div>
        <button className="page-error-retry" onClick={() => refresh()}>重试</button>
      </div>
    );
  }

  return (
    <div className="trace-workbench">
      <header className="workbench-header">
        <div>
          <h1>运行观测</h1>
          <p>Trace V2 结构、完整性与运行时机制</p>
        </div>
        <button className="icon-button" type="button" title="刷新" onClick={() => refresh()}>
          <RefreshCw size={16} />
        </button>
      </header>

      <div className="workbench-toolbar" aria-label="运行筛选">
        <select
          value={workload}
          onChange={(event) => resetFilters(() => setWorkload(event.target.value as TraceWorkload | "all"))}
          aria-label="工作负载"
        >
          {WORKLOADS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
        <select
          value={integrity}
          onChange={(event) => resetFilters(() => setIntegrity(event.target.value as TraceIntegrityStatus | "all"))}
          aria-label="完整性"
        >
          {INTEGRITIES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
        <div className="segmented-control" aria-label="时间范围">
          {(["24h", "7d", "30d", "all"] as TimeRange[]).map((range) => (
            <button
              key={range}
              type="button"
              className={timeRange === range ? "active" : ""}
              onClick={() => resetFilters(() => setTimeRange(range))}
            >
              {range === "24h" ? "24 小时" : range === "7d" ? "7 天" : range === "30d" ? "30 天" : "全部"}
            </button>
          ))}
        </div>
        <span className="toolbar-count">{total} 条运行</span>
      </div>

      <RunSection title="活跃运行" count={filteredActive.length}>
        {filteredActive.length === 0 ? (
          <EmptyState label="当前无匹配的活跃运行" />
        ) : (
          <div className="run-table-wrap">
            <table className="workbench-table">
              <RunTableHead active />
              <tbody>
                {filteredActive.map((run) => (
                  <RunRow key={run.trace_id} run={run} onOpen={() => navigate(`/traces/${run.trace_id}`)} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </RunSection>

      <RunSection title="近期运行" count={recentRuns.length}>
        {recentRuns.length === 0 ? (
          <EmptyState label="当前筛选条件下无运行" />
        ) : (
          <div className="run-table-wrap">
            <table className="workbench-table">
              <RunTableHead />
              <tbody>
                {recentRuns.map((run) => (
                  <RunRow key={run.trace_id} run={run} onOpen={() => navigate(`/traces/${run.trace_id}`)} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </RunSection>

      {total > PAGE_SIZE && (
        <div className="workbench-pager">
          <button disabled={page === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>上一页</button>
          <span>第 {page + 1} / {totalPages} 页</span>
          <button disabled={page >= totalPages - 1} onClick={() => setPage((value) => Math.min(totalPages - 1, value + 1))}>下一页</button>
        </div>
      )}
    </div>
  );
}

function RunSection({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return (
    <section className="workbench-section">
      <div className="section-heading"><h2>{title}</h2><span>{count}</span></div>
      {children}
    </section>
  );
}

function RunTableHead({ active = false }: { active?: boolean }) {
  return (
    <thead><tr>
      <th>运行</th><th>工作负载</th><th>状态</th><th>完整性</th><th>Coverage</th>
      <th>机制</th><th>{active ? "已运行" : "开始 / 耗时"}</th>
    </tr></thead>
  );
}

type ObservableRun = ActiveRun | TraceListItem;

function RunRow({ run, onOpen }: { run: ObservableRun; onOpen: () => void }) {
  return (
    <tr className="clickable-row" onClick={onOpen} tabIndex={0} onKeyDown={(event) => {
      if (event.key === "Enter" || event.key === " ") onOpen();
    }}>
      <td>
        <span className="run-primary" title={run.trace_id}>{run.session_name || run.trace_id.slice(0, 14)}</span>
        <span className="run-secondary">
          {"service" in run ? run.service || "unknown" : "live"} · v{"schema_version" in run ? run.schema_version : 2}
        </span>
      </td>
      <td><WorkloadBadge workload={run.workload} /></td>
      <td><StatusBadge status={run.status} /></td>
      <td><IntegrityBadge status={run.integrity_status} /></td>
      <td><CoverageSummary coverage={run.coverage} /></td>
      <td><MechanismSummary run={run} /></td>
      <td className="run-time-cell">
        {run.started_at ? formatDateTime(run.started_at) : "—"}
        <span>{formatMs(run.duration_ms)}</span>
      </td>
    </tr>
  );
}

function WorkloadBadge({ workload }: { workload: TraceWorkload | null }) {
  const labels: Record<TraceWorkload, string> = {
    creation: "创作", evidence_compile: "证据编译", evaluation: "评估", evolution: "进化",
  };
  return <span className={`workload-badge workload-${workload || "unknown"}`}>{workload ? labels[workload] : "未标注"}</span>;
}

function IntegrityBadge({ status }: { status: TraceIntegrityStatus }) {
  const labels: Record<TraceIntegrityStatus, string> = {
    verified: "已验证", incomplete: "不完整", conflict: "冲突", legacy: "旧版",
  };
  return (
    <span className={`integrity-badge integrity-${status}`}>
      {status !== "verified" && <AlertTriangle size={12} />}{labels[status]}
    </span>
  );
}

function StatusBadge({ status }: { status: string }) {
  return <span className={`run-status ${status}`}><span className="status-dot" />{status}</span>;
}

function CoverageSummary({ coverage }: { coverage: Record<string, string> }) {
  const values = Object.values(coverage);
  if (values.length === 0) return <span className="coverage-summary unknown">未知</span>;
  const known = values.filter((value) => value === "known" || value === "not_applicable").length;
  const complete = known === values.length;
  return <span className={`coverage-summary ${complete ? "complete" : "partial"}`}>{known}/{values.length}</span>;
}

function MechanismSummary({ run }: { run: ObservableRun }) {
  return (
    <span className="mechanism-summary" title="Skill 激活 / Middleware 干预 / HITL">
      S {run.skill_activation_count} · M {run.middleware_intervention_count} · H {run.hitl_count}
    </span>
  );
}

function EmptyState({ label }: { label: string }) {
  return <div className="workbench-empty">{label}</div>;
}

function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}
