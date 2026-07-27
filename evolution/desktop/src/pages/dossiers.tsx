import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  getTraces,
  getTraceDossiers,
  getCurrentDossier,
  startCompileDossier,
  getDossierSession,
  stopCompileDossier,
  type TraceListItem,
  type Dossier,
} from "@/lib/api";

/**
 * 证据卷宗工作台（阶段 E，需求 §31 完整工作台形态）。
 *
 * 单向加工链第一环：原始 trace → 证据编纂 Agent → 证据卷宗。
 * 用户在此选 trace → 编译卷宗 → 查看四层 + 覆盖矩阵 → 从完整卷宗启动评估。
 *
 * 布局：左栏候选 trace 列表 + 右栏卷宗工作区。
 * 候选 trace 过滤：排除 evolution_* 自观测（需求 §44）。
 */
const POLL_COMPILE_MS = 2000;
const POLL_LIST_MS = 10000;

export default function DossiersPage() {
  const navigate = useNavigate();

  const [traces, setTraces] = useState<TraceListItem[]>([]);
  const [selectedTraceId, setSelectedTraceId] = useState<string>("");
  const [dossiers, setDossiers] = useState<Dossier[]>([]);
  const [selectedDossier, setSelectedDossier] = useState<Dossier | null>(null);
  const [compiling, setCompiling] = useState(false);
  const [loadingTraces, setLoadingTraces] = useState(true);

  // 拉候选 trace 列表（排除自观测）
  const refreshTraces = useCallback(async () => {
    try {
      const resp = await getTraces({ limit: 100 });
      const filtered = resp.items.filter(
        (t) => t.run_purpose !== "evolution_eval" && t.run_purpose !== "evolution_evolve",
      );
      setTraces(filtered);
    } catch {
      /* toast 由 evoJson 统一处理 */
    } finally {
      setLoadingTraces(false);
    }
  }, []);

  useEffect(() => {
    void refreshTraces();
    const timer = setInterval(refreshTraces, POLL_LIST_MS);
    return () => clearInterval(timer);
  }, [refreshTraces]);

  // 选 trace 时拉它的卷宗版本列表
  const refreshDossiers = useCallback(async (traceId: string) => {
    if (!traceId) {
      setDossiers([]);
      setSelectedDossier(null);
      return;
    }
    try {
      const resp = await getTraceDossiers(traceId);
      setDossiers(resp.packs);
      // 默认选当前推荐版本
      const current = resp.packs.find((d) => d.is_current) || resp.packs[0] || null;
      setSelectedDossier(current);
    } catch {
      setDossiers([]);
      setSelectedDossier(null);
    }
  }, []);

  useEffect(() => {
    void refreshDossiers(selectedTraceId);
  }, [selectedTraceId, refreshDossiers]);

  // 启动编译
  const handleCompile = async () => {
    if (!selectedTraceId) return;
    setCompiling(true);
    try {
      const resp = await startCompileDossier(selectedTraceId);
      toast.success(`编译已启动：${resp.dossier_id.slice(0, 8)}`);
      // 轮询编译状态
      const poll = async () => {
        try {
          const d = await getDossierSession(resp.dossier_id);
          if (d.status === "compiling" || d.status === "pending") {
            setSelectedDossier(d);
            setTimeout(poll, POLL_COMPILE_MS);
          } else {
            setSelectedDossier(d);
            setCompiling(false);
            void refreshDossiers(selectedTraceId);
            if (d.status === "ready") toast.success("证据卷宗编译完成（完整）");
            else if (d.status === "partial") toast.warning("卷宗降级为 partial");
            else if (d.status === "failed") toast.error("编译失败：" + (d.failure_reason || ""));
          }
        } catch {
          setCompiling(false);
        }
      };
      poll();
    } catch {
      setCompiling(false);
    }
  };

  // 停止编译
  const handleStop = async (dossierId: string) => {
    if (!confirm("确认停止当前编译？")) return;
    try {
      await stopCompileDossier(dossierId);
      toast.info("已请求停止");
    } catch {
      /* ignore */
    }
  };

  // 从完整卷宗启动评估（跳评估页并预填 dossier_id）
  const handleStartEval = (dossierId: string) => {
    navigate(`/evaluation?dossier=${dossierId}`);
  };

  const manifest = selectedDossier?.manifest || null;
  const facts = selectedDossier?.facts || null;
  const coverageMatrix = manifest?.contract_coverage_matrix || null;

  return (
    <div className="page-dossiers">
      <div className="page-header">
        <h2>证据卷宗</h2>
        <p className="page-subtitle">
          原始 trace → 证据卷宗（编纂 Agent 中立加工）。完整卷宗可启动评估。
        </p>
      </div>

      <div className="dossiers-layout">
        {/* 左栏：候选 trace 列表 */}
        <div className="dossiers-sidebar">
          <h3>候选 trace（{traces.length}）</h3>
          {loadingTraces && <div className="muted">加载中…</div>}
          <div className="trace-list">
            {traces.map((t) => (
              <button
                key={t.trace_id}
                className={`trace-item ${selectedTraceId === t.trace_id ? "selected" : ""}`}
                onClick={() => setSelectedTraceId(t.trace_id)}
              >
                <span className="trace-id">{t.trace_id.slice(0, 12)}…</span>
                <span className={`trace-status ${t.status}`}>{t.status}</span>
              </button>
            ))}
            {!loadingTraces && traces.length === 0 && (
              <div className="muted">无候选 trace</div>
            )}
          </div>
        </div>

        {/* 右栏：卷宗工作区 */}
        <div className="dossiers-detail">
          {!selectedTraceId ? (
            <div className="empty-state">
              <p>← 从左侧选择一条 trace 开始编译证据卷宗</p>
            </div>
          ) : (
            <>
              <div className="detail-header">
                <h3>trace {selectedTraceId.slice(0, 16)}… 的卷宗</h3>
                <div className="detail-actions">
                  {compiling ? (
                    <button className="config-button" disabled>
                      <span className="spinner" /> 编译中…
                    </button>
                  ) : (
                    <button className="config-button primary" onClick={handleCompile}>
                      编译证据卷宗
                    </button>
                  )}
                </div>
              </div>

              {/* 卷宗版本列表 */}
              <div className="dossier-versions">
                <h4>卷宗版本（{dossiers.length}）</h4>
                {dossiers.length === 0 ? (
                  <div className="muted">尚无卷宗，点上方按钮编译</div>
                ) : (
                  <div className="version-list">
                    {dossiers.map((d) => (
                      <button
                        key={d.dossier_id}
                        className={`version-item ${selectedDossier?.dossier_id === d.dossier_id ? "selected" : ""}`}
                        onClick={() => setSelectedDossier(d)}
                      >
                        <span className={`pack-status ${d.status}`}>
                          {d.status === "ready" ? "✓ 完整" : d.status === "partial" ? "△ 降级" : d.status === "failed" ? "✗ 失败" : d.status}
                        </span>
                        <span>v{d.version}</span>
                        {d.is_current ? <span className="current-badge">当前</span> : null}
                        <span className="dossier-id">{d.dossier_id.slice(0, 8)}…</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* 选中的卷宗详情 */}
              {selectedDossier && (
                <DossierDetail
                  dossier={selectedDossier}
                  compiling={compiling}
                  manifest={manifest}
                  facts={facts}
                  coverageMatrix={coverageMatrix}
                  onStop={handleStop}
                  onStartEval={handleStartEval}
                />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── 卷宗详情子组件 ──────────────────────────────────────────────

function DossierDetail({
  dossier,
  compiling,
  manifest,
  facts,
  coverageMatrix,
  onStop,
  onStartEval,
}: {
  dossier: Dossier;
  compiling: boolean;
  manifest: any;
  facts: any;
  coverageMatrix: any;
  onStop: (id: string) => void;
  onStartEval: (id: string) => void;
}) {
  const isReady = dossier.status === "ready";
  const isCompiling = dossier.status === "compiling" || dossier.status === "pending";

  return (
    <div className="dossier-detail-panel">
      <div className="detail-grid">
        <div>
          <label>状态</label>
          <span className={`pack-status ${dossier.status}`}>{dossier.status}</span>
        </div>
        <div>
          <label>版本</label>
          <span>v{dossier.version}</span>
        </div>
        <div>
          <label>完整度</label>
          <span>{manifest?.completeness || "—"}</span>
        </div>
        <div>
          <label>provenance</label>
          <span>{dossier.provenance}</span>
        </div>
      </div>

      {dossier.failure_reason && (
        <div className="evidence-reason">⚠ {dossier.failure_reason}</div>
      )}

      {/* 任务契约覆盖矩阵（B3）*/}
      {coverageMatrix && (
        <div className="coverage-matrix-section">
          <h4>
            任务契约覆盖（complete={String(coverageMatrix.complete)}）
            <span className="matrix-counts">
              ✓{coverageMatrix.covered_count} ✗{coverageMatrix.missing_count} N/A{coverageMatrix.na_count}
            </span>
          </h4>
          <div className="matrix-items">
            {(coverageMatrix.items || []).map((item: any, i: number) => (
              <div key={i} className={`matrix-item ${item.status}`}>
                <span className="matrix-status">[{item.status}]</span>
                <span className="matrix-dim">{item.dim}/{item.key}</span>
                <span className="matrix-reason">{item.reason}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 事实层摘要（四层之一）*/}
      {facts && (
        <div className="facts-section">
          <h4>事实层摘要</h4>
          <div className="facts-grid">
            <div><label>交付物 agent</label><span>{Object.keys(facts.deliveries || {}).join(", ") || "无"}</span></div>
            <div><label>review 调用</label><span>{(facts.review_chain || []).length}</span></div>
            <div><label>revise 推断</label><span>{(facts.revise_events || []).length}</span></div>
            <div><label>错误恢复</label><span>{(facts.recovery_chain || []).length}</span></div>
            <div><label>memory_quality</label><span>{facts.memory_quality?.available ? `✓ ${facts.memory_quality.summary.total_retrievals} 次` : "无"}</span></div>
          </div>
        </div>
      )}

      {/* 操作区 */}
      <div className="detail-actions-bottom">
        {isCompiling && compiling && (
          <button className="config-button" onClick={() => onStop(dossier.dossier_id)}>
            停止编译
          </button>
        )}
        {isReady && (
          <button
            className="config-button primary"
            onClick={() => onStartEval(dossier.dossier_id)}
          >
            从此卷宗启动评估 →
          </button>
        )}
        {!isReady && !isCompiling && (
          <div className="muted">
            {dossier.status === "partial" ? "降级卷宗不可评估（需完整 ready）" :
             dossier.status === "failed" ? "失败卷宗不可评估" :
             dossier.status === "superseded" ? "已废弃版本" : "状态不可评估"}
          </div>
        )}
      </div>
    </div>
  );
}
