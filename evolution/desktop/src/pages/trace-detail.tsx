import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useNavigate, useOutletContext } from "react-router-dom";
import { GitFork, RefreshCw } from "lucide-react";
import { useTracePolling } from "@/hooks/useTraceStream";
import { TraceChainTimeline } from "@/components/trace/TraceChainTimeline";
import { TraceChainDrawer } from "@/components/trace/TraceChainDrawer";
import { TokenChartPanel } from "@/components/trace/TokenChartPanel";
import { ArtifactsPanel } from "@/components/trace/ArtifactsPanel";
import { Badge } from "@/components/ui/badge";
import {
  getTraceEvents,
  getTraceContext,
  getTraceIntegrity,
  stopActiveSession,
  resolveTrace,
} from "@/lib/api";
import type { IntegrityDiagnosis } from "@/lib/api";
import type { TraceNode, TraceRunSummary, TraceContextSegment, TraceLogEvent } from "@/lib/types";

/**
 * Trace 详情页（完全重写，替代原 156 行毛坯版本）。
 *
 * 自建页面壳（S3）：不复用 TracePanel 组件，而是直接组装 4 个纯展示子组件。
 * - 顶部：返回按钮 + trace_id + 状态 badge + 实时指示器
 * - run 概要条：状态/耗时/事件数/token
 * - tab 栏：调用链 | Token 图表
 * - tab 内容：TraceChainTimeline + Drawer  |  TokenChartPanel
 *
 * tab 切换 + 高亮联动（S6/D6）：
 * - 点击 LLM 节点的"跳转图表"按钮 → 切到 Token 图表 tab + 高亮 loopIndex
 * - Token 图表点击数据点 → 切到调用链 tab + 高亮 nodeId + 滚动定位
 */
export default function TraceDetailPage() {
  const { traceId } = useParams<{ traceId: string }>();
  const navigate = useNavigate();
  const { isSuperAdmin } = useOutletContext<{ isSuperAdmin: boolean }>();

  const {
    detail, isLive, isStale, lastSyncedAt, refresh, refreshing,
    activeSession, loading, error, availability,
  } = useTracePolling(traceId ?? null);

  // ── 页面壳状态 ──
  const [activeTab, setActiveTab] = useState<"trace" | "chart" | "artifacts">("trace");
  const [drawerNodeId, setDrawerNodeId] = useState<string | null>(null);
  const [highlightedNodeId, setHighlightedNodeId] = useState<string | null>(null);
  const [highlightedLoopIndex, setHighlightedLoopIndex] = useState<number | null>(null);

  // ── trace 稳定性重构：停止 / interrupted 收敛状态 ──
  // activeSession 由 useTracePolling 每次轮询顺便反查（保证 self_trace_id 写入后立即可见）。
  const [stopping, setStopping] = useState(false);
  const [resolving, setResolving] = useState<"failed" | "completed" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // ── 完整性结构化诊断（FR-003 / AC-004）：非 verified 时拉取具体缺口 ──
  const [integrityDiagnosis, setIntegrityDiagnosis] = useState<IntegrityDiagnosis | null>(null);
  const [integrityLoading, setIntegrityLoading] = useState(false);

  const activeRun: TraceRunSummary | null = detail?.run ?? null;
  const nodes = detail?.nodes ?? [];
  const todos = detail?.todos ?? [];

  const drawerNode: TraceNode | null = drawerNodeId
    ? nodes.find((n) => n.node_id === drawerNodeId) ?? null
    : null;

  // ── 抽屉懒加载（Phase 2 T8）──
  // events 和 context 不再随 detail 全量返回，打开抽屉时按需拉取。
  const [drawerEvents, setDrawerEvents] = useState<TraceLogEvent[]>([]);
  const [drawerContext, setDrawerContext] = useState<TraceContextSegment | null>(null);
  const [drawerLoading, setDrawerLoading] = useState(false);

  // 抽屉打开时拉取 events + context
  useEffect(() => {
    if (!drawerNode || !traceId) {
      setDrawerEvents([]);
      setDrawerContext(null);
      return;
    }

    let ignore = false;
    setDrawerLoading(true);

    (async () => {
      try {
        // 拉 node 对应的原始事件（用 raw_event_ids）
        const eventIds = drawerNode.raw_event_ids ?? [];
        let events: TraceLogEvent[] = [];
        if (eventIds.length > 0) {
          events = await getTraceEvents(traceId, eventIds);
        }

        // 拉 context（如果有 context_anchor_id）
        let contextSeg: TraceContextSegment | null = null;
        const anchorId = drawerNode.context_anchor_id;
        if (anchorId) {
          try {
            contextSeg = await getTraceContext(traceId, anchorId);
          } catch {
            // context 拉取失败不阻断（可能无 context）
          }
        }

        if (!ignore) {
          setDrawerEvents(events);
          setDrawerContext(contextSeg);
          setDrawerLoading(false);
        }
      } catch {
        if (!ignore) {
          setDrawerEvents([]);
          setDrawerContext(null);
          setDrawerLoading(false);
        }
      }
    })();

    return () => { ignore = true; };
  }, [drawerNode, traceId]);

  // 完整性诊断：run.integrity_status 非 verified 且 trace 终态时拉取。
  // FR-008：pending（记录中/封存中）是中性态，不拉诊断、不当故障展示（EVD-005 根因）。
  // 失败时有限重试（最多 3 次，每次退避），避免永久停在"诊断信息暂时不可用"（AC-004）。
  useEffect(() => {
    const status = activeRun?.integrity_status;
    const isTerminal = activeRun?.status
      ? !["running", "awaiting_input", "pending", "cancelling"].includes(activeRun.status)
      : false;
    // pending 不拉诊断（记录中/校验中，由 banner 显示中性态）；verified 不需要诊断。
    if (!traceId || !status || status === "verified" || status === "pending" || !isTerminal) {
      setIntegrityDiagnosis(null);
      return;
    }
    let ignore = false;
    let attempt = 0;
    const MAX_ATTEMPTS = 3;
    const backoffMs = [1500, 3000, 6000];
    setIntegrityLoading(true);
    const fetchDiag = async () => {
      try {
        const diag = await getTraceIntegrity(traceId);
        if (!ignore) {
          setIntegrityDiagnosis(diag);
          setIntegrityLoading(false);
        }
      } catch {
        if (ignore) return;
        attempt += 1;
        if (attempt >= MAX_ATTEMPTS) {
          // 重试用尽：保留状态标签，显示可重试的故障态（EDGE-003 诊断服务故障独立语义）。
          setIntegrityDiagnosis(null);
          setIntegrityLoading(false);
        } else {
          // 退避重试（诊断依赖的 receipt/manifest 可能正在封存中，稍后可读）。
          setTimeout(fetchDiag, backoffMs[attempt - 1]);
        }
      }
    };
    fetchDiag();
    return () => { ignore = true; };
  }, [traceId, activeRun?.integrity_status, activeRun?.status]);

  // 抽屉内 LLM 输入消息（从懒加载的 events 找 start 事件的 input.messages）
  const drawerInputMessages = useMemo(() => {
    if (!drawerNode || drawerNode.kind !== "llm") return null;
    if (drawerEvents.length === 0) return null;
    const startId = drawerNode.raw_event_ids[0];
    if (!startId) return null;
    const startEvent = drawerEvents.find((e) => e.event_id === startId);
    if (!startEvent?.input || typeof startEvent.input !== "object") return null;
    const input = startEvent.input as { messages?: unknown[] };
    return Array.isArray(input.messages) ? input.messages : null;
  }, [drawerNode, drawerEvents]);

  // 抽屉内节点输出（从懒加载的 events 找 end 事件的 output/tool_output）
  const drawerNodeOutput = useMemo(() => {
    if (!drawerNode || drawerEvents.length === 0) return null;
    if (drawerNode.raw_event_ids.length < 2) return null;
    const endId = drawerNode.raw_event_ids[drawerNode.raw_event_ids.length - 1];
    const endEvent = drawerEvents.find((e) => e.event_id === endId);
    if (!endEvent) return null;

    if (drawerNode.kind === "llm") {
      if (!endEvent.output || typeof endEvent.output !== "object") return null;
      const output = endEvent.output as { messages?: unknown[] };
      return Array.isArray(output.messages) ? output.messages : null;
    }
    if (drawerNode.kind === "tool") {
      return endEvent.tool_output ?? null;
    }
    return null;
  }, [drawerNode, drawerEvents]);

  // 抽屉 context（单 segment 懒加载，不再从 detail.context 全量取）
  const context = drawerContext ? [drawerContext] : [];

  // ── 交互回调 ──

  function selectNode(node: TraceNode) {
    if (node.kind === "llm" || node.kind === "todo" || node.kind === "error" || node.kind === "skill") {
      setDrawerNodeId(node.node_id);
    }
  }

  function closeDrawer() {
    setDrawerNodeId(null);
  }

  // 图表 → 调用链跳转：切 tab + 高亮节点
  const handleJumpToTrace = useCallback((nodeId: string) => {
    setActiveTab("trace");
    setHighlightedNodeId(nodeId);
    setHighlightedLoopIndex(null);
  }, []);

  // 调用链 → 图表跳转：切 tab + 高亮 loopIndex
  const handleJumpToChart = useCallback((loopIndex: number) => {
    setActiveTab("chart");
    setHighlightedLoopIndex(loopIndex);
    setHighlightedNodeId(null);
  }, []);

  const clearHighlight = useCallback(() => {
    setHighlightedNodeId(null);
    setHighlightedLoopIndex(null);
  }, []);

  // traceId 变化时清除高亮 + 关抽屉
  useEffect(() => {
    clearHighlight();
    setDrawerNodeId(null);
    setActiveTab("trace");
  }, [traceId, clearHighlight]);

  // 点"停止"：调 stopActiveSession 分发到对应 stop 接口。
  const handleStop = useCallback(async () => {
    if (!activeSession || stopping) return;
    if (!window.confirm("确认停止当前 trace 对应的 session？")) return;
    setStopping(true);
    setActionError(null);
    try {
      const result = await stopActiveSession(activeSession);
      if (!result.ok) {
        setActionError(result.error ?? "停止失败");
      }
      // 停止成功后：下个轮询 tick（≤1s）会看到状态变 cancelled，按钮自动隐藏。
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "停止失败");
    } finally {
      setStopping(false);
    }
  }, [activeSession, stopping]);

  // 点"标记为失败/完成"：收敛 interrupted trace。
  const handleResolve = useCallback(
    async (target: "failed" | "completed") => {
      if (!traceId || resolving) return;
      const hint = target === "failed" ? "失败" : "完成";
      if (!window.confirm(`确认将此 trace 标记为${hint}？`)) return;
      setResolving(target);
      setActionError(null);
      try {
        await resolveTrace(traceId, target);
        // 收敛成功后：下个轮询 tick 会看到状态变化（但 interrupted 是终态，轮询已停；
        // 这里手动 refresh 一次确保 UI 立即更新）。
        window.location.reload();
      } catch (err) {
        setActionError(err instanceof Error ? err.message : "收敛失败");
        setResolving(null);
      }
    },
    [traceId, resolving],
  );

  // ── 渲染 ──

  if (loading) {
    return <div className="page-loading">加载 trace…</div>;
  }

  // FR-003：准备态（跨进程/摄入竞态，自动刷新）与不可用态（断链旧记录），
  // 不呈现原始 not found。available 态走下面的正常渲染分支。
  if (!detail && (availability === "preparing" || availability === "unavailable")) {
    return (
      <div className="trace-detail-page">
        <header className="trace-detail-header">
          <button className="back-button" onClick={() => navigate(-1)}>← 返回</button>
        </header>
        {availability === "preparing" ? (
          <div className="trace-detail-empty">Trace 准备中…</div>
        ) : (
          <div className="trace-detail-empty">Trace 不可用，可重跑</div>
        )}
      </div>
    );
  }

  if (error && !detail) {
    return (
      <div className="trace-detail-page">
        <header className="trace-detail-header">
          <button className="back-button" onClick={() => navigate(-1)}>← 返回</button>
        </header>
        <div className="trace-detail-error">{error}</div>
      </div>
    );
  }

  if (!detail || !activeRun) {
    return (
      <div className="trace-detail-page">
        <header className="trace-detail-header">
          <button className="back-button" onClick={() => navigate(-1)}>← 返回</button>
        </header>
        <div className="trace-detail-empty">trace 不存在</div>
      </div>
    );
  }

  const run = activeRun;

  return (
    <div className="trace-detail-page">
      {/* 顶部 */}
      <header className="trace-detail-header">
        <div className="trace-detail-title-row">
          <button className="back-button" onClick={() => navigate(-1)}>← 返回</button>
          <h1>Trace {traceId?.slice(0, 12)}…</h1>
          <StatusBadge status={run.status} />
          <button
            type="button"
            className="lineage-jump-button"
            onClick={() => navigate(`/lineage?type=trace&id=${encodeURIComponent(traceId || "")}`)}
          >
            <GitFork size={14} />血缘
          </button>
          {isLive && !isStale && (
            <span className="streaming-badge"><span className="pulse" /> 实时</span>
          )}
          {isStale && (
            // FR-002/EDGE-003：同步失败保留旧快照但明确标陈旧，不伪装成实时。
            <span className="stale-badge" title={error ?? undefined}>陈旧</span>
          )}
          {/* DEC-004：常驻刷新按钮 + 最后成功同步时间。进行中防重复并发（AC-004）。 */}
          <button
            type="button"
            className="refresh-button"
            disabled={refreshing || loading}
            onClick={refresh}
            title={refreshing ? "同步中…" : "立即刷新拉取最新 Trace"}
          >
            <RefreshCw size={14} className={refreshing ? "spin" : undefined} />
            {refreshing ? "同步中" : "刷新"}
          </button>
          {lastSyncedAt && (
            <span className="last-synced" title="最后成功同步时间">
              同步于 {new Date(lastSyncedAt).toLocaleTimeString("zh-CN", { hour12: false })}
            </span>
          )}
          {/* trace 稳定性重构：running/cancelling 时显示停止按钮；interrupted 时显示收敛按钮。
              FR-006：cancelling 时按钮显示"取消中…"（受理后立即可见），允许重复点击幂等重试。 */}
          {(run.status === "running" || run.status === "cancelling") && activeSession?.session_id && (
            <button
              type="button"
              className="stop-button"
              disabled={stopping || run.status === "cancelling"}
              onClick={handleStop}
              title={run.status === "cancelling" ? "取消收敛中，请等待终态" : `停止 ${activeSession.session_type} session`}
            >
              {run.status === "cancelling" ? "取消中…" : stopping ? "停止中…" : "停止"}
            </button>
          )}
          {run.status === "interrupted" && (
            <div className="resolve-actions">
              <button
                type="button"
                className="resolve-button resolve-failed"
                disabled={resolving !== null}
                onClick={() => handleResolve("failed")}
              >
                {resolving === "failed" ? "标记中…" : "标记为失败"}
              </button>
              <button
                type="button"
                className="resolve-button resolve-completed"
                disabled={resolving !== null}
                onClick={() => handleResolve("completed")}
              >
                {resolving === "completed" ? "标记中…" : "标记为完成"}
              </button>
            </div>
          )}
        </div>
        {actionError && (
          <div className="action-error">{actionError}</div>
        )}
        {run.status === "interrupted" && run.interrupted_reason && (
          <div className="interrupted-hint">
            此 trace 已中断（原因：{interruptedReasonLabel(run.interrupted_reason)}）。
            trace 数据完整保留，请根据实际情况手动标记结果。
          </div>
        )}
        {run.integrity_status === "pending" && (
          // FR-008/AC-010：pending 是中性态（记录中/封存中），不是数据损坏。
          // 不显示故障横幅，只提示用户等待封存校验（EVD-005 根因：运行中不得误报 incomplete）。
          <div className="integrity-hint integrity-pending">
            <span>Trace 完整性：{integrityLabel(run.integrity_status)}。运行结束后将自动校验，这不是数据损坏，无需重新运行。</span>
          </div>
        )}
        {run.integrity_status && run.integrity_status !== "verified" && run.integrity_status !== "pending" && (
          <div className={`integrity-hint integrity-${run.integrity_status}`}>
            {integrityLoading ? (
              <span>正在分析完整性缺口…</span>
            ) : integrityDiagnosis && integrityDiagnosis.missing_checks.length > 0 ? (
              <div className="integrity-diagnosis">
                <div className="integrity-diagnosis-summary">
                  Trace 完整性：{integrityLabel(run.integrity_status)}。
                  {integrityDiagnosis.affected_downstreams.length > 0 && (
                    <>受影响下游：{integrityDiagnosis.affected_downstreams.map(downstreamLabel).join("、")}。</>
                  )}
                </div>
                <ul className="integrity-check-list">
                  {integrityDiagnosis.missing_checks.map((check) => (
                    <li key={check.check} className="integrity-check-item">
                      <span className="integrity-check-stage">{check.stage}</span>
                      <span className="integrity-check-impact">{check.impact}</span>
                      <span className="integrity-check-recovery">恢复：{check.recovery}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <span>
                Trace 完整性：{integrityLabel(run.integrity_status)}，下游证据、评估和进化流程不可消费。
                {run.integrity_status === "legacy"
                  ? " 该 Trace 由旧版系统生成，建议重新运行生成 V2 Trace。"
                  : " 诊断信息暂时不可用，请稍后刷新或重新运行该任务。"}
              </span>
            )}
          </div>
        )}
      </header>

      {/* run 概要条 */}
      <section className="trace-detail-summary">
        <div className="summary-item">
          <label>耗时</label>
          <span>{run.duration_ms != null ? formatDuration(run.duration_ms) : "—"}</span>
        </div>
        <div className="summary-item">
          <label>事件数</label>
          <span>{run.event_count}</span>
        </div>
        <div className="summary-item">
          <label>开始</label>
          <span>{run.started_at?.replace("T", " ").slice(0, 19) || "—"}</span>
        </div>
        <div className="summary-item">
          <label>节点数</label>
          <span>{nodes.filter((n) => n.kind !== "run").length}</span>
        </div>
        <div className="summary-item">
          <label>工作负载</label>
          <span>{workloadLabel(run.workload)}</span>
        </div>
        <div className="summary-item">
          <label>完整性</label>
          <StatusBadge status={run.integrity_status || "legacy"} />
        </div>
        <div className="summary-item">
          <label>Coverage</label>
          <span>{coverageLabel(run.coverage || {})}</span>
        </div>
        {run.error && (
          <div className="summary-item error">
            <label>错误</label>
            <span>{run.error}</span>
          </div>
        )}
      </section>

      {/* tab 栏 */}
      <nav className="inspection-tabs">
        <button
          className={`inspection-tab ${activeTab === "trace" ? "active" : ""}`}
          type="button"
          onClick={() => setActiveTab("trace")}
        >
          调用链
        </button>
        <button
          className={`inspection-tab ${activeTab === "chart" ? "active" : ""}`}
          type="button"
          onClick={() => setActiveTab("chart")}
        >
          Token 图表
        </button>
        <button
          className={`inspection-tab ${activeTab === "artifacts" ? "active" : ""}`}
          type="button"
          onClick={() => setActiveTab("artifacts")}
        >
          产物
        </button>
      </nav>

      {/* tab 内容 */}
      <div className="trace-detail-body">
        {activeTab === "trace" && (
          <div className="trace-layout trace-chain-layout">
            <TraceChainTimeline
              nodes={nodes}
              activeRun={activeRun}
              activeNodeId={drawerNodeId ?? ""}
              onSelectNode={selectNode}
              onJumpToChart={handleJumpToChart}
              highlightedNodeId={highlightedNodeId}
              onClearHighlight={clearHighlight}
            />
            <TraceChainDrawer
              node={drawerNode}
              context={context}
              todos={todos}
              inputMessages={drawerInputMessages}
              nodeOutput={drawerNodeOutput}
              onClose={closeDrawer}
            />
          </div>
        )}
        {activeTab === "chart" && (
          <TokenChartPanel
            detail={detail}
            hasActiveThread={true}
            activeTraceId={traceId ?? ""}
            onJumpToTrace={handleJumpToTrace}
            highlightedLoopIndex={highlightedLoopIndex}
            onClearHighlight={clearHighlight}
          />
        )}
        {activeTab === "artifacts" && (
          <ArtifactsPanel traceId={traceId ?? ""} canReadContent={isSuperAdmin} />
        )}
      </div>
    </div>
  );
}

// ── 辅助组件 ──

function StatusBadge({ status }: { status: string }) {
  const variant =
    status === "completed" ? "completed"
    : status === "failed" ? "failed"
    : status === "interrupted" ? "interrupted"
    : status === "cancelled" ? "cancelled"
    : status === "cancelling" ? "cancelled"
    : status === "cancel_timeout" ? "failed"
    : "running";
  const label =
    status === "completed" ? "完成"
    : status === "failed" ? "失败"
    : status === "interrupted" ? "已中断"
    : status === "cancelled" ? "已取消"
    : status === "cancelling" ? "取消中"
    : status === "cancel_timeout" ? "取消超时"
    : status === "awaiting_input" ? "等待输入"
    : "运行中";
  return <Badge variant={variant as any}>{label}</Badge>;
}

/** interrupted_reason 中文标签（trace 稳定性重构）。 */
function interruptedReasonLabel(reason: string): string {
  if (reason === "process_restart") return "进程重启";
  if (reason === "heartbeat_timeout") return "心跳超时";
  if (reason === "user_marked") return "用户标记";
  return reason;
}

function integrityLabel(status: string): string {
  if (status === "verified") return "已验证";
  if (status === "pending") return "记录中（待校验）";
  if (status === "incomplete") return "不完整";
  if (status === "conflict") return "冲突";
  return "旧版未验证";
}

function downstreamLabel(downstream: string): string {
  if (downstream === "evidence_compile") return "证据卷宗编纂";
  if (downstream === "evaluation") return "评估";
  if (downstream === "evolution") return "进化";
  return downstream;
}

function workloadLabel(workload: string | null | undefined): string {
  if (workload === "creation") return "创作";
  if (workload === "evidence_compile") return "证据编译";
  if (workload === "evaluation") return "评估";
  if (workload === "evolution") return "进化";
  return "未标注";
}

function coverageLabel(coverage: Record<string, string>): string {
  const values = Object.values(coverage);
  if (values.length === 0) return "未知";
  const known = values.filter((value) => value === "known" || value === "not_applicable").length;
  return `${known}/${values.length}`;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  if (ms < 3_600_000) return `${(ms / 60_000).toFixed(ms < 600_000 ? 1 : 0)}min`;
  return `${(ms / 3_600_000).toFixed(1)}h`;
}
