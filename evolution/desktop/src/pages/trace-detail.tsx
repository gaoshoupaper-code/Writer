import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTraceStream } from "@/hooks/useTraceStream";
import { TraceChainTimeline } from "@/components/trace/TraceChainTimeline";
import { TraceChainDrawer } from "@/components/trace/TraceChainDrawer";
import { TokenChartPanel } from "@/components/trace/TokenChartPanel";
import { Badge } from "@/components/ui/badge";
import { getTraceEvents, getTraceContext } from "@/lib/api";
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

  const { detail, isLive, loading, error } = useTraceStream(traceId ?? null);

  // ── 页面壳状态 ──
  const [activeTab, setActiveTab] = useState<"trace" | "chart">("trace");
  const [drawerNodeId, setDrawerNodeId] = useState<string | null>(null);
  const [highlightedNodeId, setHighlightedNodeId] = useState<string | null>(null);
  const [highlightedLoopIndex, setHighlightedLoopIndex] = useState<number | null>(null);

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

  // ── 渲染 ──

  if (loading) {
    return <div className="page-loading">加载 trace…</div>;
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
          {isLive && (
            <span className="streaming-badge"><span className="pulse" /> 实时</span>
          )}
        </div>
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
      </nav>

      {/* tab 内容 */}
      <div className="trace-detail-body">
        {activeTab === "trace" ? (
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
        ) : (
          <TokenChartPanel
            detail={detail}
            hasActiveThread={true}
            activeTraceId={traceId ?? ""}
            onJumpToTrace={handleJumpToTrace}
            highlightedLoopIndex={highlightedLoopIndex}
            onClearHighlight={clearHighlight}
          />
        )}
      </div>
    </div>
  );
}

// ── 辅助组件 ──

function StatusBadge({ status }: { status: string }) {
  const variant = status === "completed" ? "completed" : status === "failed" ? "failed" : "running";
  const label = status === "completed" ? "完成" : status === "failed" ? "失败" : status === "awaiting_input" ? "等待输入" : status === "cancelled" ? "已取消" : "运行中";
  return <Badge variant={variant as any}>{label}</Badge>;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  if (ms < 3_600_000) return `${(ms / 60_000).toFixed(ms < 600_000 ? 1 : 0)}min`;
  return `${(ms / 3_600_000).toFixed(1)}h`;
}
