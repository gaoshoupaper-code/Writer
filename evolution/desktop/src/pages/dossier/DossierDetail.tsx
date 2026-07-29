/**
 * 右栏卷宗工作区：结论摘要常驻 + 版本导航 + 分层 Tabs（FR-001 / FR-003 / FR-004）。
 *
 * 信息架构三层（DEC-003）：
 *  1. 结论摘要卡片（常驻主层，FR-001）：当前 trace 名 + 版本 + 完整性结论 + 下一步主操作
 *  2. 版本导航条：横向版本卡片，可滚动
 *  3. Tabs 分层区（FR-003）：覆盖情况 / 事实摘要 / 语义归纳 / 技术信息
 *
 * Tabs 切换纯本地 state，不发网络请求（NFR-003）。
 * 工程字段（provenance / rule_version / llm_calls 等）下沉到「技术信息」Tab（FR-001 次层化）。
 */

import { useState } from "react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Search, FileSearch, Sparkles, Settings2 } from "lucide-react";

import type { Dossier } from "@/lib/api";

import {
  getDossierStatusMeta,
  getCompleteness,
  type StatusTone,
} from "./dossierHelpers";
import { CoverageTab } from "./tabs/CoverageTab";
import { FactsTab } from "./tabs/FactsTab";
import { SemanticTab } from "./tabs/SemanticTab";
import { TechnicalTab } from "./tabs/TechnicalTab";
import type { DrillTarget } from "./EvidenceSheet";

interface DossierDetailProps {
  traceId: string;
  dossiers: Dossier[];
  selectedDossier: Dossier | null;
  compiling: boolean;
  onSelectDossier: (d: Dossier) => void;
  onCompile: () => void;
  onStop: (dossierId: string) => void;
  onStartEval: (dossierId: string) => void;
  onDrill: (target: DrillTarget) => void;
}

// 四个 Tab 的固定标识
type TabValue = "coverage" | "facts" | "semantic" | "technical";

export function DossierDetail({
  traceId,
  dossiers,
  selectedDossier,
  compiling,
  onSelectDossier,
  onCompile,
  onStop,
  onStartEval,
  onDrill,
}: DossierDetailProps) {
  const [activeTab, setActiveTab] = useState<TabValue>("coverage");

  const dossier = selectedDossier;
  const statusMeta = dossier ? getDossierStatusMeta(dossier.status) : null;
  const completeness = dossier ? getCompleteness(dossier.manifest) : "";

  return (
    <div className="dossier-detail-panel">
      {/* ── 结论摘要卡片（FR-001 主层，常驻）── */}
      <DossierSummaryCard
        traceId={traceId}
        dossier={dossier}
        compiling={compiling}
        completeness={completeness}
        statusLabel={statusMeta?.label ?? ""}
        statusTone={statusMeta?.tone ?? "muted"}
        onCompile={onCompile}
        onStop={onStop}
        onStartEval={onStartEval}
      />

      {/* ── 版本导航条 ── */}
      <DossierVersionBar
        dossiers={dossiers}
        selectedId={dossier?.dossier_id ?? ""}
        onSelect={onSelectDossier}
      />

      {/* ── 分层 Tabs（FR-003）── */}
      {dossier ? (
        <Tabs
          value={activeTab}
          onValueChange={(v) => setActiveTab(v as TabValue)}
          className="dossier-tabs"
        >
          <TabsList>
            <TabsTrigger value="coverage">
              <Search size={13} aria-hidden /> 覆盖情况
            </TabsTrigger>
            <TabsTrigger value="facts">
              <FileSearch size={13} aria-hidden /> 事实摘要
            </TabsTrigger>
            <TabsTrigger value="semantic">
              <Sparkles size={13} aria-hidden /> 语义归纳
            </TabsTrigger>
            <TabsTrigger value="technical">
              <Settings2 size={13} aria-hidden /> 技术信息
            </TabsTrigger>
          </TabsList>

          <TabsContent value="coverage">
            <CoverageTab dossier={dossier} />
          </TabsContent>
          <TabsContent value="facts">
            <FactsTab dossier={dossier} onDrill={onDrill} />
          </TabsContent>
          <TabsContent value="semantic">
            <SemanticTab dossier={dossier} onDrill={onDrill} />
          </TabsContent>
          <TabsContent value="technical">
            <TechnicalTab dossier={dossier} onDrill={onDrill} />
          </TabsContent>
        </Tabs>
      ) : (
        <div className="dossier-tabs-empty">
          <p>尚无卷宗，点上方「编译证据卷宗」开始</p>
        </div>
      )}
    </div>
  );
}

// ── 结论摘要卡片 ──────────────────────────────────────────────

interface DossierSummaryCardProps {
  traceId: string;
  dossier: Dossier | null;
  compiling: boolean;
  completeness: string;
  statusLabel: string;
  statusTone: StatusTone;
  onCompile: () => void;
  onStop: (dossierId: string) => void;
  onStartEval: (dossierId: string) => void;
}

function DossierSummaryCard({
  traceId,
  dossier,
  compiling,
  completeness,
  statusLabel,
  statusTone,
  onCompile,
  onStop,
  onStartEval,
}: DossierSummaryCardProps) {
  // trace 标题：需要从父层传入的 traceId 推断（此处无 TraceListItem，用截断 ID）
  // 真正的可读名在左栏候选列表已展示；这里给出截断 ID 作为锚点
  const traceAnchor = traceId.slice(0, 16) + "…";

  return (
    <div className="dossier-summary-card">
      <div className="dossier-summary-head">
        <div className="dossier-summary-id">
          <span className="dossier-summary-label">当前 trace</span>
          <span className="dossier-summary-trace" title={traceId}>
            {traceAnchor}
          </span>
        </div>
        {dossier && (
          <div className="dossier-summary-version">
            <span className="dossier-summary-label">版本</span>
            <span className="dossier-summary-versionnum">v{dossier.version}</span>
            {dossier.is_current && <span className="dossier-summary-current">当前</span>}
          </div>
        )}
      </div>

      <div className="dossier-summary-body">
        {dossier ? (
          <>
            <span className={`dossier-status-badge tone-${statusTone}`}>
              {statusLabel}
            </span>
            {completeness && (
              <span className="dossier-summary-completeness">
                完整度：<strong>{completeness === "full" ? "完整" : "部分"}</strong>
              </span>
            )}
            {dossier.failure_reason && (
              <span className="dossier-summary-reason">⚠ {dossier.failure_reason}</span>
            )}
          </>
        ) : (
          <span className="dossier-summary-noversion">该 trace 尚无卷宗</span>
        )}
      </div>

      <div className="dossier-summary-action">
        {compiling ? (
          <button className="config-button" disabled>
            <span className="spinner" /> 编译中…
          </button>
        ) : dossier && statusMetaCompiling(dossier) ? (
          <button className="config-button" onClick={() => onStop(dossier.dossier_id)}>
            停止编译
          </button>
        ) : !dossier ? (
          <button className="config-button primary" onClick={onCompile}>
            编译证据卷宗
          </button>
        ) : dossier.status === "ready" ? (
          <button
            className="config-button primary"
            onClick={() => onStartEval(dossier.dossier_id)}
          >
            从此卷宗启动评估 →
          </button>
        ) : (
          <span className="dossier-summary-blocked">
            {getBlockedReason(dossier)}
          </span>
        )}
      </div>
    </div>
  );
}

// 卷宗是否处于编译中态（dossier 维度，区别于页面级 compiling）
function statusMetaCompiling(d: Dossier): boolean {
  return d.status === "compiling" || d.status === "pending";
}

// 不可评估时的中文原因（CON-002：产品用户无需解读工程字段即可判断）
function getBlockedReason(d: Dossier): string {
  switch (d.status) {
    case "partial":
      return "降级卷宗不可评估（需完整 ready 才能进入评估）";
    case "failed":
      return "失败卷宗不可评估";
    case "superseded":
      return "该版本已被后续版本取代（已废弃）";
    case "cancelled":
      return "该卷宗编译已被取消";
    default:
      return "当前状态不可评估";
  }
}

// ── 版本导航条 ────────────────────────────────────────────────

interface DossierVersionBarProps {
  dossiers: Dossier[];
  selectedId: string;
  onSelect: (d: Dossier) => void;
}

function DossierVersionBar({ dossiers, selectedId, onSelect }: DossierVersionBarProps) {
  if (dossiers.length === 0) return null;

  return (
    <div className="dossier-version-bar">
      <span className="dossier-version-bar-label">版本（{dossiers.length}）</span>
      <div className="dossier-version-list">
        {dossiers.map((d) => {
          const meta = getDossierStatusMeta(d.status);
          const Icon = meta.icon;
          const isSelected = d.dossier_id === selectedId;
          return (
            <button
              key={d.dossier_id}
              className={`dossier-version-item ${isSelected ? "selected" : ""}`}
              onClick={() => onSelect(d)}
              aria-current={isSelected ? "true" : undefined}
              title={`${meta.label} · v${d.version}`}
            >
              <span className={`dossier-version-status tone-${meta.tone}`}>
                <Icon size={11} className={meta.tone === "info" ? "spin" : ""} aria-hidden />
              </span>
              <span className="dossier-version-num">v{d.version}</span>
              {d.is_current && <span className="dossier-version-current">当前</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}
