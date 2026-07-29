/**
 * Tab「事实摘要」— 事实层（FR-004）。
 *
 * 按 extractor.py 产出的真实类别组织 facts：
 *  - 运行摘要 + 契约（用户目标 / 任务类型 / 承诺产物 / 缺失项）
 *  - 交付物（agent → 文件，冻结正文可点开抽屉核验）
 *  - review / revise 链（evidence_id 可点受控回钻）
 *  - 错误恢复链
 *  - 资源与记忆质量
 *
 * 某类别为空时显示局部空态，不隐藏其他类别，也不把空值呈现为零缺口（FR-003）。
 */

import {
  FileText,
  GitBranch,
  RefreshCw,
  Brain,
  ClipboardList,
  Package,
} from "lucide-react";

import type { Dossier } from "@/lib/api";
import { getFactsView, isEmptyValue } from "../dossierHelpers";
import type { DrillTarget } from "../EvidenceSheet";

interface FactsTabProps {
  dossier: Dossier;
  onDrill: (target: DrillTarget) => void;
}

export function FactsTab({ dossier, onDrill }: FactsTabProps) {
  const facts = dossier.facts;
  if (!facts || isEmptyValue(facts)) {
    return (
      <div className="dossier-tab-empty">
        <FileText size={24} aria-hidden />
        <p>该卷宗未生成事实层</p>
      </div>
    );
  }

  const view = getFactsView(facts);

  return (
    <div className="facts-tab">
      {/* 契约摘要 */}
      <FactsSection icon={<ClipboardList size={14} aria-hidden />} title="任务契约">
        {view.contract ? (
          <div className="facts-contract">
            {view.contract.userGoal && (
              <div className="facts-row">
                <span className="facts-row-label">用户目标</span>
                <span className="facts-row-value">{view.contract.userGoal}</span>
              </div>
            )}
            {view.contract.taskType && (
              <div className="facts-row">
                <span className="facts-row-label">任务类型</span>
                <span className="facts-row-value">{view.contract.taskType}</span>
              </div>
            )}
            {view.contract.promisedArtifacts.length > 0 && (
              <div className="facts-row">
                <span className="facts-row-label">承诺产物</span>
                <span className="facts-row-value">
                  {view.contract.promisedArtifacts.join("、")}
                </span>
              </div>
            )}
            {view.contract.missing.length > 0 ? (
              <div className="facts-row facts-row-warn">
                <span className="facts-row-label">契约缺失项</span>
                <span className="facts-row-value">{view.contract.missing.join("、")}</span>
              </div>
            ) : (
              view.contract.available && (
                <div className="facts-row facts-row-ok">
                  <span className="facts-row-label">契约完整性</span>
                  <span className="facts-row-value">契约可用，无缺失项</span>
                </div>
              )
            )}
            {!view.contract.available && (
              <div className="facts-row facts-row-warn">
                <span className="facts-row-label">契约完整性</span>
                <span className="facts-row-value">该 trace 未提取到可用任务契约</span>
              </div>
            )}
          </div>
        ) : (
          <EmptyHint text="未提取到任务契约" />
        )}
      </FactsSection>

      {/* 交付物（冻结正文可点开抽屉核验） */}
      <FactsSection icon={<Package size={14} aria-hidden />} title={`交付物（${view.deliveries.length} 个 agent）`}>
        {view.deliveries.length > 0 ? (
          <div className="facts-deliveries">
            {view.deliveries.map((d) => (
              <div key={d.agent} className="facts-delivery-group">
                <div className="facts-delivery-agent">{d.agent}</div>
                {d.files.map((f) => (
                  <button
                    key={f.path}
                    className="facts-delivery-file"
                    onClick={() =>
                      onDrill({
                        kind: "artifact",
                        path: f.path,
                        content: f.contentFrozen,
                        charCount: f.charCount,
                        truncated: f.truncated,
                      })
                    }
                    title="点击查看冻结正文"
                  >
                    <FileText size={12} aria-hidden />
                    <span className="facts-delivery-path">{f.path}</span>
                    {f.charCount != null && (
                      <span className="facts-delivery-chars">{f.charCount.toLocaleString()} 字</span>
                    )}
                    {f.truncated && <span className="facts-delivery-trunc">截断</span>}
                    <span className="facts-delivery-action">查看</span>
                  </button>
                ))}
              </div>
            ))}
          </div>
        ) : (
          <EmptyHint text="无冻结交付物" />
        )}
      </FactsSection>

      {/* review / revise 链（evidence_id 可点回钻） */}
      <FactsSection
        icon={<GitBranch size={14} aria-hidden />}
        title={`review / revise（${view.reviewChain.length} 次调用 / ${view.reviseEvents.length} 次推断）`}
      >
        {view.reviewChain.length > 0 || view.reviseEvents.length > 0 ? (
          <div className="facts-review-list">
            {view.reviewChain.map((r, i) => (
              <div key={`rv-${i}`} className="facts-review-item">
                <div className="facts-review-head">
                  <span className="facts-review-reviewer">{r.reviewer || "reviewer"}</span>
                  <span className="facts-review-seq">#{r.sequence ?? i + 1}</span>
                  {r.isRecheck && <span className="facts-review-tag">复查</span>}
                </div>
                {r.reviewFile && (
                  <div className="facts-review-file">{r.reviewFile}</div>
                )}
                {r.findings && (
                  <div className="facts-review-findings">{r.findings}</div>
                )}
                {r.evidenceId && (
                  <EvidenceDrillButton
                    dossierId={dossier.dossier_id}
                    evidenceId={r.evidenceId}
                    label="回钻原始事件"
                    onDrill={onDrill}
                  />
                )}
              </div>
            ))}
            {view.reviseEvents.map((rv, i) => (
              <div key={`rev-${i}`} className="facts-review-item facts-revise">
                <div className="facts-review-head">
                  <span className="facts-review-reviewer">{rv.subagent || "revise"}</span>
                  <span className="facts-review-tag facts-review-tag-inferred">推断</span>
                </div>
                {rv.revisedPath && (
                  <div className="facts-review-file">{rv.revisedPath}</div>
                )}
                {rv.note && <div className="facts-review-findings">{rv.note}</div>}
                {rv.evidenceId && (
                  <EvidenceDrillButton
                    dossierId={dossier.dossier_id}
                    evidenceId={rv.evidenceId}
                    label="回钻原始事件"
                    onDrill={onDrill}
                  />
                )}
              </div>
            ))}
          </div>
        ) : (
          <EmptyHint text="本次运行无 review / revise 记录" />
        )}
      </FactsSection>

      {/* 错误恢复链 */}
      <FactsSection
        icon={<RefreshCw size={14} aria-hidden />}
        title={`错误恢复（${view.recoveryChain.length} 条）`}
      >
        {view.recoveryChain.length > 0 ? (
          <div className="facts-recovery-list">
            {view.recoveryChain.map((r, i) => (
              <div key={`rc-${i}`} className="facts-recovery-item">
                <div className="facts-recovery-type">{r.errorType || "未知错误"}</div>
                {r.agentName && (
                  <div className="facts-recovery-agent">{r.agentName}</div>
                )}
                {r.errorMessage && (
                  <div className="facts-recovery-msg">{r.errorMessage}</div>
                )}
                {r.evidenceId && (
                  <EvidenceDrillButton
                    dossierId={dossier.dossier_id}
                    evidenceId={r.evidenceId}
                    label="回钻原始事件"
                    onDrill={onDrill}
                  />
                )}
              </div>
            ))}
          </div>
        ) : (
          <EmptyHint text="本次运行无错误恢复记录" />
        )}
      </FactsSection>

      {/* 记忆质量 */}
      <FactsSection icon={<Brain size={14} aria-hidden />} title="记忆与资源">
        {view.memoryQuality ? (
          <div className="facts-memory">
            {view.memoryQuality.available ? (
              <>
                <div className="facts-row">
                  <span className="facts-row-label">检索次数</span>
                  <span className="facts-row-value">
                    {view.memoryQuality.totalRetrievals}（成功 {view.memoryQuality.okCount} / 失败 {view.memoryQuality.failCount}）
                  </span>
                </div>
                {view.memoryQuality.totalNodes != null && (
                  <div className="facts-row">
                    <span className="facts-row-label">记忆节点</span>
                    <span className="facts-row-value">{view.memoryQuality.totalNodes}</span>
                  </div>
                )}
                {view.memoryQuality.totalEdges != null && (
                  <div className="facts-row">
                    <span className="facts-row-label">记忆边</span>
                    <span className="facts-row-value">{view.memoryQuality.totalEdges}</span>
                  </div>
                )}
              </>
            ) : (
              <EmptyHint text="本次运行未启用记忆检索" />
            )}
          </div>
        ) : (
          <EmptyHint text="无记忆质量数据" />
        )}
      </FactsSection>
    </div>
  );
}

// ── 子组件 ────────────────────────────────────────────────────

function FactsSection({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="facts-section">
      <h4 className="facts-section-title">
        {icon}
        {title}
      </h4>
      {children}
    </section>
  );
}

function EmptyHint({ text }: { text: string }) {
  return <div className="facts-empty-hint">{text}</div>;
}

// 受控回钻按钮：点击后在抽屉加载该 evidence 的原始事件
function EvidenceDrillButton({
  dossierId,
  evidenceId,
  label,
  onDrill,
}: {
  dossierId: string;
  evidenceId: string;
  label: string;
  onDrill: (target: DrillTarget) => void;
}) {
  return (
    <button
      className="facts-evidence-drill"
      onClick={() => onDrill({ kind: "evidence", dossierId, evidenceId })}
    >
      <GitBranch size={11} aria-hidden />
      <code>{evidenceId}</code>
      <span className="facts-evidence-drill-label">{label}</span>
    </button>
  );
}
