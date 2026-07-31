import type { EvolveArchitectureIssue } from "@/lib/api";

/**
 * 待处理架构问题面板（DEC-006 / FR-008 / AC-007）。
 *
 * 展示进化 Agent 上报的"六要素外、agent 够不着"的架构层问题，让产品负责人可见可处置。
 * 与进化点（PointsDrawer）区分：进化点是 agent 可改要素的改进建议；架构问题是 agent
 * 上报的跨层根因，交人类决策（不硬修）。
 *
 * 信息：归属层 / 问题描述 / 证据引用 / agent 判断理由 / 上报时间 / session 关联。
 * 空表显示空状态。
 */
interface Props {
  issues: EvolveArchitectureIssue[];
  pendingCount: number;
}

const LAYER_LABEL: Record<EvolveArchitectureIssue["layer"], string> = {
  assembly: "装配入口",
  executor: "executor 端",
  framework: "第三方框架",
  "eval-infra": "评估 infra",
  "data-pipeline": "数据管道",
};

const STATUS_LABEL: Record<EvolveArchitectureIssue["status"], string> = {
  reported: "待处理",
  acknowledged: "已确认",
  resolved: "已解决",
};

const STATUS_ICON: Record<EvolveArchitectureIssue["status"], string> = {
  reported: "▲",
  acknowledged: "◐",
  resolved: "✓",
};

export default function ArchitectureIssuesPanel({ issues, pendingCount }: Props) {
  return (
    <section className="arch-issues-panel" aria-label="待处理架构问题">
      <header className="panel-header">
        <h3 className="panel-title">待处理架构问题</h3>
        {pendingCount > 0 && (
          <span className="panel-badge">{pendingCount}</span>
        )}
      </header>

      {issues.length === 0 ? (
        <p className="panel-empty">暂无架构层问题上报。Agent 遇到六要素外的根因会在此上报。</p>
      ) : (
        <ul className="issue-list">
          {issues.map((issue) => (
            <li key={issue.id} className={`issue-item issue-${issue.status}`}>
              <div className="issue-row">
                <span className="issue-icon" aria-hidden>
                  {STATUS_ICON[issue.status]}
                </span>
                <span className="issue-layer">{LAYER_LABEL[issue.layer]}</span>
                <span className="issue-status">{STATUS_LABEL[issue.status]}</span>
                <span className="issue-seq">#{issue.seq}</span>
              </div>
              <p className="issue-problem">{issue.problem}</p>
              <p className="issue-evidence">
                证据：<code>{issue.evidence_ref}</code>
              </p>
              {issue.note && <p className="issue-note">Agent 判断：{issue.note}</p>}
              <p className="issue-meta">
                上报于 {new Date(issue.created_at).toLocaleString()}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
