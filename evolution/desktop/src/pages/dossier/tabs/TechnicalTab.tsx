/**
 * Tab「技术信息」— 索引层 + 工程元数据（FR-001 次层化 + FR-004 索引层）。
 *
 * 这是工程字段的归宿：主层（结论摘要 / 覆盖 / 事实 / 语义）不再暴露工程字段，
 * 全部下沉到这里供需要时核验：
 *  - 索引层：可回钻的 evidence_ids（点击受控回钻）、产物路径索引
 *  - 工程元数据：provenance / compile_rule_version / llm_calls_used / dossier_id / 时间戳
 *
 * CON-005：所有标识按纯文本呈现。
 */

import { useState } from "react";
import { Settings2, Link2, Clock, Copy, Check, Hash } from "lucide-react";

import type { Dossier } from "@/lib/api";
import { getIndexView } from "../dossierHelpers";
import type { DrillTarget } from "../EvidenceSheet";

interface TechnicalTabProps {
  dossier: Dossier;
  onDrill: (target: DrillTarget) => void;
}

export function TechnicalTab({ dossier, onDrill }: TechnicalTabProps) {
  const indexView = getIndexView(dossier.index);

  return (
    <div className="technical-tab">
      {/* 索引层 */}
      <TechnicalSection icon={<Link2 size={14} aria-hidden />} title="证据索引">
        {indexView.evidenceIds.length > 0 ? (
          <div className="technical-evidence-list">
            {indexView.evidenceIds.map((eid) => (
              <button
                key={eid}
                className="technical-evidence-id"
                onClick={() =>
                  onDrill({ kind: "evidence", dossierId: dossier.dossier_id, evidenceId: eid })
                }
                title="点击回钻原始事件"
              >
                <Hash size={11} aria-hidden />
                <code>{eid}</code>
              </button>
            ))}
          </div>
        ) : (
          <EmptyHint text="卷宗索引为空" />
        )}
      </TechnicalSection>

      <TechnicalSection icon={<Link2 size={14} aria-hidden />} title={`产物路径（${indexView.artifactPaths.length}）`}>
        {indexView.artifactPaths.length > 0 ? (
          <div className="technical-paths">
            {indexView.artifactPaths.map((p) => (
              <div key={p} className="technical-path" title={p}>
                <code>{p}</code>
              </div>
            ))}
          </div>
        ) : (
          <EmptyHint text="无产物路径索引" />
        )}
      </TechnicalSection>

      {/* 工程元数据 */}
      <TechnicalSection icon={<Settings2 size={14} aria-hidden />} title="工程元数据">
        <div className="technical-meta">
          <MetaRow label="dossier_id" value={dossier.dossier_id} copyable />
          <MetaRow label="trace_id" value={dossier.trace_id} copyable />
          <MetaRow label="provenance" value={dossier.provenance} />
          <MetaRow label="compile_rule_version" value={dossier.compile_rule_version} />
          <MetaRow label="llm_calls_used" value={String(dossier.llm_calls_used)} />
          <MetaRow label="version" value={`v${dossier.version}`} />
          <MetaRow label="is_current" value={String(dossier.is_current)} />
          <MetaRow label="status" value={dossier.status} />
          {dossier.failure_reason && (
            <MetaRow label="failure_reason" value={dossier.failure_reason} />
          )}
        </div>
      </TechnicalSection>

      {/* 时间线 */}
      <TechnicalSection icon={<Clock size={14} aria-hidden />} title="时间线">
        <div className="technical-meta">
          <MetaRow label="created_at" value={dossier.created_at || "—"} />
          <MetaRow label="finished_at" value={dossier.finished_at || "—"} />
        </div>
      </TechnicalSection>
    </div>
  );
}

// ── 子组件 ────────────────────────────────────────────────────

function TechnicalSection({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="technical-section">
      <h4 className="technical-section-title">
        {icon}
        {title}
      </h4>
      {children}
    </section>
  );
}

function EmptyHint({ text }: { text: string }) {
  return <div className="technical-empty-hint">{text}</div>;
}

function MetaRow({
  label,
  value,
  copyable,
}: {
  label: string;
  value: string;
  copyable?: boolean;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 剪贴板不可用时静默 */
    }
  };

  return (
    <div className="technical-meta-row">
      <span className="technical-meta-label">{label}</span>
      <code className="technical-meta-value" title={value}>
        {value}
      </code>
      {copyable && (
        <button
          className="technical-meta-copy"
          onClick={handleCopy}
          aria-label={`复制 ${label}`}
          title="复制"
        >
          {copied ? <Check size={12} aria-hidden /> : <Copy size={12} aria-hidden />}
        </button>
      )}
    </div>
  );
}
