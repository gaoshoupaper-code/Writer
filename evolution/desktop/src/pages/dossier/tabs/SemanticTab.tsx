/**
 * Tab「语义归纳」— 语义层（FR-004）。
 *
 * 语义层是编纂 Agent 的归纳，不是确定性事实。
 * 顶部必须显式标注这一点，避免把归纳伪装为事实（CON：语义层不得伪装为事实）。
 *
 * 四种状态：
 *  - ok：正常展示阶段摘要 + P0/P1/P2 重点
 *  - skipped：LLM 未配置，如实显示「未生成」
 *  - error：归纳异常，如实显示原因
 *  - limited：LLM 调用达上限，显示「部分阶段未归纳」
 *
 * key_facts / priorities 的 evidence_id 可点受控回钻。
 */

import { Sparkles, Info, AlertCircle, AlertTriangle } from "lucide-react";

import type { Dossier } from "@/lib/api";
import { getSemanticView, isEmptyValue } from "../dossierHelpers";
import type { DrillTarget } from "../EvidenceSheet";

interface SemanticTabProps {
  dossier: Dossier;
  onDrill: (target: DrillTarget) => void;
}

export function SemanticTab({ dossier, onDrill }: SemanticTabProps) {
  const semantic = dossier.semantic;
  if (!semantic || isEmptyValue(semantic)) {
    return (
      <div className="dossier-tab-empty">
        <Sparkles size={24} aria-hidden />
        <p>该卷宗未生成语义层</p>
        <p className="dossier-tab-empty-hint">历史卷宗可能不含语义归纳。</p>
      </div>
    );
  }

  const view = getSemanticView(semantic);

  // 降级状态：如实显示，不生成替代性结论（FR-004 失败语义）
  if (view.status === "skipped" || view.status === "error" || view.status === "limited") {
    return (
      <div className="semantic-tab">
        <SemanticBanner status={view.status} reason={view.reason} />
        {/* 即使降级，已归纳的阶段仍展示（limited 场景下可能有部分阶段） */}
        {view.stages.length > 0 && <StagesBlock stages={view.stages} dossierId={dossier.dossier_id} onDrill={onDrill} />}
      </div>
    );
  }

  // ok
  return (
    <div className="semantic-tab">
      <div className="semantic-disclaimer">
        <Info size={13} aria-hidden />
        <span>
          以下为编纂 Agent 对本次运行的归纳，<strong>非确定性事实</strong>；
          带证据引用的条目可回钻原始事件核验。
        </span>
      </div>

      <StagesBlock stages={view.stages} dossierId={dossier.dossier_id} onDrill={onDrill} />

      {view.priorities.length > 0 && (
        <PrioritiesBlock priorities={view.priorities} dossierId={dossier.dossier_id} onDrill={onDrill} />
      )}
    </div>
  );
}

// ── 降级横幅 ──────────────────────────────────────────────────

function SemanticBanner({
  status,
  reason,
}: {
  status: "skipped" | "error" | "limited";
  reason: string | null;
}) {
  const Icon = status === "error" ? AlertCircle : AlertTriangle;
  const title =
    status === "skipped"
      ? "语义层未生成"
      : status === "error"
        ? "语义归纳失败"
        : "语义归纳不完整";
  return (
    <div className={`semantic-banner semantic-banner-${status}`}>
      <Icon size={16} aria-hidden />
      <div>
        <div className="semantic-banner-title">{title}</div>
        {reason && <div className="semantic-banner-reason">{reason}</div>}
      </div>
    </div>
  );
}

// ── 阶段摘要 ──────────────────────────────────────────────────

function StagesBlock({
  stages,
  dossierId,
  onDrill,
}: {
  stages: ReturnType<typeof getSemanticView>["stages"];
  dossierId: string;
  onDrill: (target: DrillTarget) => void;
}) {
  if (stages.length === 0) return null;
  return (
    <section className="semantic-section">
      <h4 className="semantic-section-title">阶段归纳</h4>
      <div className="semantic-stages">
        {stages.map((st, i) => (
          <div key={`st-${i}`} className="semantic-stage">
            <div className="semantic-stage-name">{st.name || `阶段 ${i + 1}`}</div>
            {st.summary && <div className="semantic-stage-summary">{st.summary}</div>}
            {st.keyFacts.length > 0 && (
              <ul className="semantic-keyfacts">
                {st.keyFacts.map((kf, j) => (
                  <li key={`kf-${j}`} className="semantic-keyfact">
                    <span className="semantic-keyfact-text">{kf.fact}</span>
                    {kf.evidenceId && (
                      <button
                        className="semantic-keyfact-drill"
                        onClick={() =>
                          onDrill({ kind: "evidence", dossierId, evidenceId: kf.evidenceId! })
                        }
                      >
                        <code>{kf.evidenceId}</code>
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

// ── P0/P1/P2 重点 ─────────────────────────────────────────────

const PRIORITY_META: Record<string, { label: string; tone: string }> = {
  P0: { label: "P0 · 关键", tone: "danger" },
  P1: { label: "P1 · 重要", tone: "warning" },
  P2: { label: "P2 · 参考", tone: "muted" },
};

function PrioritiesBlock({
  priorities,
  dossierId,
  onDrill,
}: {
  priorities: ReturnType<typeof getSemanticView>["priorities"];
  dossierId: string;
  onDrill: (target: DrillTarget) => void;
}) {
  // 按 P0 → P1 → P2 分组
  const groups: Record<string, typeof priorities> = { P0: [], P1: [], P2: [] };
  const others: typeof priorities = [];
  for (const p of priorities) {
    if (groups[p.level]) groups[p.level].push(p);
    else others.push(p);
  }

  return (
    <section className="semantic-section">
      <h4 className="semantic-section-title">全局重点（按优先级）</h4>
      <div className="semantic-priorities">
        {(["P0", "P1", "P2"] as const).map((level) => {
          const items = groups[level];
          if (items.length === 0) return null;
          const meta = PRIORITY_META[level];
          return (
            <div key={level} className="semantic-priority-group">
              <div className="semantic-priority-head">
                <span className={`semantic-priority-level tone-${meta.tone}`}>{meta.label}</span>
                <span className="semantic-priority-count">{items.length}</span>
              </div>
              <ul className="semantic-priority-items">
                {items.map((p, i) => (
                  <li key={`p-${level}-${i}`} className="semantic-priority-item">
                    {p.title && <div className="semantic-priority-title">{p.title}</div>}
                    {p.detail && <div className="semantic-priority-detail">{p.detail}</div>}
                    {p.evidenceId && (
                      <button
                        className="semantic-keyfact-drill"
                        onClick={() =>
                          onDrill({ kind: "evidence", dossierId, evidenceId: p.evidenceId! })
                        }
                      >
                        <code>{p.evidenceId}</code>
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
        {others.length > 0 && (
          <div className="semantic-priority-group">
            <div className="semantic-priority-head">
              <span className="semantic-priority-level tone-muted">其他</span>
              <span className="semantic-priority-count">{others.length}</span>
            </div>
            <ul className="semantic-priority-items">
              {others.map((p, i) => (
                <li key={`p-other-${i}`} className="semantic-priority-item">
                  {p.title && <div className="semantic-priority-title">{p.title}</div>}
                  {p.detail && <div className="semantic-priority-detail">{p.detail}</div>}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}
