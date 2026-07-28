import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, RefreshCw } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { getWorkloadProfiles } from "@/lib/api";
import type { TraceWorkload, WorkloadProfile, WorkloadProfilesResponse } from "@/lib/types";

const WORKLOAD_LABELS: Record<TraceWorkload, string> = {
  creation: "创作",
  evidence_compile: "证据编译",
  evaluation: "评估",
  evolution: "进化",
};
const WINDOWS = [
  { hours: 24, label: "24 小时" },
  { hours: 168, label: "7 天" },
  { hours: 720, label: "30 天" },
  { hours: 2160, label: "90 天" },
  { hours: 8760, label: "12 个月" },
];

export default function AnalysisPage() {
  const navigate = useNavigate();
  const [hours, setHours] = useState(720);
  const [selected, setSelected] = useState<TraceWorkload>("creation");
  const [data, setData] = useState<WorkloadProfilesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setData(await getWorkloadProfiles(hours));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "分析数据加载失败");
    } finally {
      setLoading(false);
    }
  }, [hours]);

  useEffect(() => { void refresh(); }, [refresh]);

  const profile = useMemo(
    () => data?.profiles.find((item) => item.workload === selected) || null,
    [data, selected],
  );

  if (loading && !data) return <div className="page-loading">加载分析数据…</div>;
  if (error && !data) {
    return (
      <div className="page-error">
        <div className="page-error-title">分析数据加载失败</div>
        <div className="page-error-detail">{error}</div>
        <button className="page-error-retry" onClick={refresh}>重试</button>
      </div>
    );
  }

  return (
    <div className="trace-workbench analysis-workbench">
      <header className="workbench-header">
        <div><h1>分析</h1><p>按 workload 隔离的 Trace Profile</p></div>
        <button className="icon-button" type="button" title="刷新" onClick={refresh} disabled={loading}>
          <RefreshCw size={16} />
        </button>
      </header>

      <div className="analysis-meta-bar">
        <div className="segmented-control" aria-label="分析时间窗">
          {WINDOWS.map((window) => (
            <button key={window.hours} className={hours === window.hours ? "active" : ""} onClick={() => setHours(window.hours)}>
              {window.label}
            </button>
          ))}
        </div>
        <span>公式 <code>{data?.formula_version || "—"}</code></span>
        <span>起始 {data?.window.started_after ? formatDateTime(data.window.started_after) : "—"}</span>
      </div>

      <div className="profile-tabs" role="tablist" aria-label="工作负载 Profile">
        {data?.profiles.map((item) => (
          <button
            key={item.workload}
            type="button"
            role="tab"
            aria-selected={selected === item.workload}
            className={selected === item.workload ? "active" : ""}
            onClick={() => setSelected(item.workload)}
          >
            <span>{WORKLOAD_LABELS[item.workload]}</span><strong>{item.sample_size}</strong>
          </button>
        ))}
      </div>

      {profile && <ProfileView profile={profile} onOpenTrace={(traceId) => navigate(`/traces/${traceId}`)} />}
    </div>
  );
}

function ProfileView({ profile, onOpenTrace }: { profile: WorkloadProfile; onOpenTrace: (id: string) => void }) {
  const coverageFields = Object.entries(profile.coverage.fields);
  return (
    <>
      <section className="profile-summary-band">
        <Metric label="样本量" value={formatNumber(profile.sample_size)} detail="当前 workload" />
        <Metric label="成功率" value={formatPercent(profile.success_rate)} detail={`分母 ${profile.status_denominator}`} />
        <Metric label="完整性" value={`${profile.integrity.verified}/${profile.integrity.denominator}`} detail="verified / 全部" />
        <Metric label="Coverage" value={`${profile.coverage.complete}/${profile.coverage.denominator}`} detail="完整 / 全部" />
        <Metric label="P50" value={formatMs(profile.duration_ms.p50)} detail={`P90 ${formatMs(profile.duration_ms.p90)}`} />
        <Metric label="Token" value={formatNumber(profile.usage.total_tokens)} detail={`最大深度 ${profile.usage.max_span_depth}`} />
      </section>

      <div className="analysis-columns">
        <section className="workbench-section">
          <div className="section-heading"><h2>状态与完整性</h2><span>独立分母</span></div>
          <table className="workbench-table compact-table">
            <thead><tr><th>维度</th><th>分类</th><th>数量</th><th>分母</th></tr></thead>
            <tbody>
              {Object.entries(profile.statuses).map(([status, count]) => (
                <tr key={`status-${status}`}><td>运行状态</td><td>{status}</td><td>{count}</td><td>{profile.sample_size}</td></tr>
              ))}
              {(["verified", "incomplete", "conflict", "legacy"] as const).map((status) => (
                <tr key={`integrity-${status}`}><td>完整性</td><td>{status}</td><td>{profile.integrity[status]}</td><td>{profile.integrity.denominator}</td></tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="workbench-section">
          <div className="section-heading"><h2>运行时机制</h2><span>显式事件</span></div>
          <table className="workbench-table compact-table">
            <thead><tr><th>机制</th><th>事件数</th></tr></thead>
            <tbody>
              <tr><td>Skill 激活</td><td>{profile.mechanisms.skill_activations}</td></tr>
              <tr><td>Middleware 干预</td><td>{profile.mechanisms.middleware_interventions}</td></tr>
              <tr><td>重试</td><td>{profile.mechanisms.retries}</td></tr>
              <tr><td>HITL</td><td>{profile.mechanisms.hitl_events}</td></tr>
              <tr><td>输入 Token</td><td>{formatNumber(profile.usage.input_tokens)}</td></tr>
              <tr><td>输出 Token</td><td>{formatNumber(profile.usage.output_tokens)}</td></tr>
            </tbody>
          </table>
        </section>
      </div>

      <section className="workbench-section">
        <div className="section-heading"><h2>Coverage 分母</h2><span>{coverageFields.length} 个字段</span></div>
        {coverageFields.length === 0 ? <div className="workbench-empty">无 coverage 字段</div> : (
          <div className="run-table-wrap"><table className="workbench-table compact-table">
            <thead><tr><th>字段</th><th>known</th><th>partial</th><th>unknown</th><th>not applicable</th><th>分母</th></tr></thead>
            <tbody>{coverageFields.map(([field, counts]) => (
              <tr key={field}>
                <td><code>{field}</code></td><td>{counts.known || 0}</td><td>{counts.partial || 0}</td>
                <td>{counts.unknown || 0}</td><td>{counts.not_applicable || 0}</td><td>{profile.coverage.denominator}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </section>

      <section className="workbench-section advanced-readiness">
        <div className="section-heading"><h2>高级分析数据条件</h2></div>
        {profile.advanced_analysis.eligible ? (
          <div className="readiness-ok"><CheckCircle2 size={16} />数据条件满足</div>
        ) : (
          <div className="readiness-missing">
            <AlertCircle size={16} />
            <div>{profile.advanced_analysis.missing_conditions.map((condition) => (
              <span key={condition}>{conditionLabel(condition)}</span>
            ))}</div>
          </div>
        )}
      </section>

      <section className="workbench-section">
        <div className="section-heading"><h2>回钻样本</h2><span>{profile.drilldown_trace_ids.length}</span></div>
        {profile.drilldown_trace_ids.length === 0 ? <div className="workbench-empty">无回钻样本</div> : (
          <div className="drilldown-list">{profile.drilldown_trace_ids.map((traceId) => (
            <button key={traceId} onClick={() => onOpenTrace(traceId)}><code>{traceId}</code></button>
          ))}</div>
        )}
      </section>
    </>
  );
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <dl className="profile-metric"><dt>{label}</dt><dd>{value}</dd><small>{detail}</small></dl>;
}

function conditionLabel(condition: string): string {
  const labels: Record<string, string> = {
    "coverage>=95%": "Coverage 至少 95%",
    sealed_evaluation_sample: "封存评估样本",
    versioned_rubric: "版本化 Rubric",
    linked_outcome: "可关联 Outcome",
    completed_experiment: "完成且可复查的实验",
    "workload=creation": "仅创作质量 Profile 适用",
  };
  return labels[condition] || (condition.startsWith("sample_size>=") ? `样本量至少 ${condition.split(">=")[1]}` : condition);
}

function formatNumber(value: number): string { return new Intl.NumberFormat("zh-CN").format(value); }
function formatPercent(value: number | null): string { return value == null ? "—" : `${(value * 100).toFixed(1)}%`; }
function formatMs(value: number | null): string { return value == null ? "—" : value < 1000 ? `${value}ms` : `${(value / 1000).toFixed(1)}s`; }
function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
