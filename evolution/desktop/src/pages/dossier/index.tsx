/**
 * 证据卷宗工作台（阶段 E，需求 §31 完整工作台形态）。
 *
 * 单向加工链第一环：原始 trace → 证据编纂 Agent → 证据卷宗。
 * 用户在此选 trace → 编译卷宗 → 查看四层 + 覆盖矩阵 → 从完整卷宗启动评估。
 *
 * 布局：左栏候选 trace 列表（搜索/筛选）+ 右栏卷宗工作区（结论摘要 + 版本导航 + 分层 Tabs）。
 * 候选 trace 过滤：排除 evolution_* 自观测（需求 §44）。
 *
 * 业务不变量（CON-004）：编译幂等、版本不可变、ready 才能评估、owner 权限、四层后端契约——
 * 这些数据流与原实现完全一致，本次仅重组呈现层。
 */

import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import {
  getDossierCandidates,
  getTraceDossiers,
  startCompileDossier,
  getDossierSession,
  stopCompileDossier,
  type TraceListItem,
  type Dossier,
} from "@/lib/api";

import { DossierSidebar } from "./DossierSidebar";
import { DossierDetail } from "./DossierDetail";
import { EvidenceSheet, type DrillTarget } from "./EvidenceSheet";

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

  // 回钻抽屉状态（FR-005/DEC-005）：当前核验对象。
  // 抽屉是独立 overlay，关闭后不影响 trace/版本/Tab 选中态。
  const [drillTarget, setDrillTarget] = useState<DrillTarget | null>(null);

  // 拉候选 trace 列表（排除自观测）
  const refreshTraces = useCallback(async () => {
    try {
      const resp = await getDossierCandidates(100);
      setTraces(resp.items);
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

  return (
    <div className="page-dossiers">
      <div className="page-header">
        <h1>证据卷宗</h1>
        <p className="page-desc">
          原始 trace → 证据卷宗（编纂 Agent 中立加工）。完整卷宗可启动评估。
        </p>
      </div>

      <div className="dossiers-layout">
        {/* 左栏：候选 trace 搜索 + 筛选 + 列表 */}
        <DossierSidebar
          traces={traces}
          loading={loadingTraces}
          selectedTraceId={selectedTraceId}
          onSelect={setSelectedTraceId}
        />

        {/* 右栏：卷宗工作区 */}
        <div className="dossiers-detail">
          {!selectedTraceId ? (
            <div className="dossiers-empty">
              <p>从左侧选择一条 trace 开始编译证据卷宗</p>
              <p className="dossiers-empty-hint">
                候选列表已按最近记录优先排列，支持按名称或 ID 搜索、按状态筛选。
              </p>
            </div>
          ) : (
            <DossierDetail
              traceId={selectedTraceId}
              dossiers={dossiers}
              selectedDossier={selectedDossier}
              compiling={compiling}
              onSelectDossier={setSelectedDossier}
              onCompile={handleCompile}
              onStop={handleStop}
              onStartEval={handleStartEval}
              onDrill={setDrillTarget}
            />
          )}
        </div>
      </div>

      {/* 回钻抽屉（FR-005）：独立 overlay，受控回钻证据或冻结产物 */}
      <EvidenceSheet target={drillTarget} onClose={() => setDrillTarget(null)} />
    </div>
  );
}
