import { useCallback, useEffect, useMemo, useState } from "react";
import type { TraceDetail, TraceNode, TraceRunSummary } from "../../lib/types";
import { TokenChartPanel } from "./TokenChartPanel";
import { TraceChainTimeline } from "./TraceChainTimeline";
import { TraceChainDrawer } from "./TraceChainDrawer";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";

type TracePanelProps = {
  runs: TraceRunSummary[];
  detail: TraceDetail | null;
  activeTraceId: string;
  loading: boolean;
  hasActiveThread: boolean;
  deletingTraceId: string;
  onSelectTrace: (traceId: string) => void;
  onDeleteTrace: (traceId: string) => void;
};

const NODE_LABELS: Record<string, string> = {
  run: "Run",
  agent: "Agent",
  llm: "LLM",
  tool: "Tool",
  todo: "Todo",
  error: "Error",
};

export function TracePanel({ runs, detail, activeTraceId, loading, hasActiveThread, deletingTraceId, onSelectTrace, onDeleteTrace }: TracePanelProps) {
  const [activeNodeId, setActiveNodeId] = useState("");
  const [activeTab, setActiveTab] = useState<"trace" | "chart">("trace");
  const [drawerNodeId, setDrawerNodeId] = useState<string | null>(null);

  const [highlightedNodeId, setHighlightedNodeId] = useState<string | null>(null);
  const [highlightedLoopIndex, setHighlightedLoopIndex] = useState<number | null>(null);

  const activeRun = runs.find((run) => run.trace_id === activeTraceId) ?? null;
  const nodes = detail?.run.trace_id === activeTraceId ? detail.nodes : [];
  const context = detail?.run.trace_id === activeTraceId ? detail.context : [];
  const todos = detail?.run.trace_id === activeTraceId ? detail.todos : [];
  const drawerNode = drawerNodeId ? nodes.find((n) => n.node_id === drawerNodeId) ?? null : null;

  const drawerInputMessages = useMemo(() => {
    if (!drawerNode || !detail || drawerNode.kind !== "llm") return null;
    const startId = drawerNode.raw_event_ids[0];
    if (!startId) return null;
    const startEvent = detail.events.find((e) => e.event_id === startId);
    if (!startEvent?.input || typeof startEvent.input !== "object") return null;
    const input = startEvent.input as { messages?: unknown[] };
    return Array.isArray(input.messages) ? input.messages : null;
  }, [drawerNode, detail]);

  const drawerNodeOutput = useMemo(() => {
    if (!drawerNode || !detail) return null;
    if (drawerNode.raw_event_ids.length < 2) return null;
    const endId = drawerNode.raw_event_ids[drawerNode.raw_event_ids.length - 1];
    const endEvent = detail.events.find((e) => e.event_id === endId);
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
  }, [drawerNode, detail]);

  function selectNode(node: TraceNode) {
    setActiveNodeId(node.node_id);
    if (node.kind === "llm" || node.kind === "todo" || node.kind === "error") {
      setDrawerNodeId(node.node_id);
    }
  }

  function closeDrawer() {
    setDrawerNodeId(null);
  }

  const handleJumpToTrace = useCallback((nodeId: string) => {
    setActiveTab("trace");
    setHighlightedNodeId(nodeId);
    setHighlightedLoopIndex(null);
  }, []);

  const handleJumpToChart = useCallback((loopIndex: number) => {
    setActiveTab("chart");
    setHighlightedLoopIndex(loopIndex);
    setHighlightedNodeId(null);
  }, []);

  const clearHighlight = useCallback(() => {
    setHighlightedNodeId(null);
    setHighlightedLoopIndex(null);
  }, []);

  useEffect(() => {
    clearHighlight();
  }, [activeTraceId]);

  const statusVariant = (status: string): "running" | "completed" | "failed" => {
    if (status === "completed") return "completed";
    if (status === "failed") return "failed";
    return "running";
  };

  return (
    <section className="content-panel panel-surface trace-panel">
      <header className="panel-heading">
        <div>
          <span className="section-kicker">Inspection System</span>
          <h2>检测系统</h2>
        </div>
        <div className="trace-heading-actions">
          <TraceDropdownSelector runs={runs} activeTraceId={activeTraceId} onSelect={onSelectTrace} />
          <Badge variant={activeRun ? statusVariant(activeRun.status) : "muted"}>
            {activeRun ? statusLabel(activeRun.status) : "等待 Trace"}
          </Badge>
          <button
            className="trace-delete-button"
            type="button"
            onClick={() => onDeleteTrace(activeTraceId)}
            disabled={!activeRun || activeRun.status === "running" || deletingTraceId === activeTraceId}
            title={activeRun?.status === "running" ? "运行中的 Trace 不能删除" : "删除当前 Trace"}
          >
            {deletingTraceId === activeTraceId ? "删除中..." : "删除 Trace"}
          </button>
        </div>
      </header>

      {/* 保持原始 DOM 结构：nav + 两个 div，确保 3 行 grid 正确 */}
      <nav className="inspection-tabs">
        <button className={`inspection-tab ${activeTab === "trace" ? "active" : ""}`} type="button" onClick={() => setActiveTab("trace")}>执行追踪</button>
        <button className={`inspection-tab ${activeTab === "chart" ? "active" : ""}`} type="button" onClick={() => setActiveTab("chart")}>图检测</button>
      </nav>

      <div className="trace-panel-body" style={activeTab !== "trace" ? { display: "none" } : undefined}>
        {loading && nodes.length === 0 ? (
          <div style={{ padding: 20, display: "grid", gap: 12 }}>
            <Skeleton className="h-8 w-48" />
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-20 w-3/4" />
          </div>
        ) : !hasActiveThread ? (
          <div className="empty-state trace-empty-state">
            <span className="card-line" />
            <h3>先选择一个会话</h3>
            <p>Trace 记录按会话归档，选择会话后才能查看执行历史。</p>
          </div>
        ) : null}

        {hasActiveThread && (nodes.length > 0 || !loading) ? (
          <div className="trace-layout trace-chain-layout">
            <TraceChainTimeline
              nodes={nodes}
              activeRun={activeRun}
              activeNodeId={activeNodeId}
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
        ) : null}
      </div>

      <div style={activeTab !== "chart" ? { display: "none" } : undefined}>
        <TokenChartPanel
          detail={detail}
          hasActiveThread={hasActiveThread}
          activeTraceId={activeTraceId}
          onJumpToTrace={handleJumpToTrace}
          highlightedLoopIndex={highlightedLoopIndex}
          onClearHighlight={clearHighlight}
        />
      </div>
    </section>
  );
}

// ── TraceDropdownSelector: 使用 shadcn DropdownMenu ──

function TraceDropdownSelector({ runs, activeTraceId, onSelect }: { runs: TraceRunSummary[]; activeTraceId: string; onSelect: (traceId: string) => void }) {
  const activeRun = runs.find((r) => r.trace_id === activeTraceId);

  if (runs.length === 0) {
    return <span className="text-xs font-bold text-muted-foreground">无 Trace 记录</span>;
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="trace-dropdown-trigger" type="button">
          <span className="trace-dropdown-trigger-text">
            {activeRun ? activeRun.session_name || activeRun.endpoint : "选择 Trace"}
          </span>
          <span className="trace-dropdown-arrow">▾</span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[320px]">
        {runs.map((run) => (
          <DropdownMenuItem
            key={run.trace_id}
            className={run.trace_id === activeTraceId ? "bg-primary/5" : ""}
            onClick={() => onSelect(run.trace_id)}
          >
            <span className="flex-shrink-0 text-xs">
              {run.trace_id === activeTraceId ? "●" : "○"}
            </span>
            <span className="flex flex-col gap-0.5 min-w-0">
              <strong className="text-xs truncate">{run.session_name || run.endpoint}</strong>
              <small className="text-[11px] text-muted-foreground">
                {formatTime(run.started_at)} · {statusLabel(run.status)} · {run.event_count} events
              </small>
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// ── 辅助函数 ──

function statusLabel(status: TraceRunSummary["status"]) {
  if (status === "completed") return "完成";
  if (status === "failed") return "失败";
  return "运行中";
}

function safeClassName(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9_-]/g, "-");
}

function compactAgentName(value: string) {
  return value.replace(/-subagent$/, "").replace(/-agent$/, "");
}

function nodeBodyLabel(node: TraceNode) {
  return node.model_name || node.tool_name || node.label || (NODE_LABELS[node.kind] ?? node.kind);
}

function formatDuration(value: number) {
  if (value < 1000) return `${value}ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}s`;
  if (value < 3_600_000) return `${(value / 60_000).toFixed(value < 600_000 ? 1 : 0)}min`;
  return `${(value / 3_600_000).toFixed(1)}h`;
}

function formatTime(value: string) {
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}
