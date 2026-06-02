import { useMemo, useState } from "react";
import type { TraceContextSegment, TraceDetail, TraceNode, TraceRunSummary, TraceTodoItem } from "../../lib/types";

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

const SEGMENT_LABELS: Record<string, string> = {
  system: "内部上下文",
  human: "用户",
  ai: "AI",
  tool: "Tool",
  todo: "Todo",
  error: "错误",
};

type TraceSegmentPhase = "input" | "output" | "error" | "context";

type TraceMetricItem = {
  key: string;
  label: string;
  tone?: string;
};

export function TracePanel({ runs, detail, activeTraceId, loading, hasActiveThread, deletingTraceId, onSelectTrace, onDeleteTrace }: TracePanelProps) {
  const [activeNodeId, setActiveNodeId] = useState("");
  const activeRun = runs.find((run) => run.trace_id === activeTraceId) ?? null;
  const nodes = detail?.run.trace_id === activeTraceId ? detail.nodes : [];
  const context = detail?.run.trace_id === activeTraceId ? detail.context : [];
  const activeNode = nodes.find((node) => node.node_id === activeNodeId) ?? null;

  function selectNode(node: TraceNode) {
    setActiveNodeId(node.node_id);
    const anchorId = node.input_context_range?.start_anchor_id ?? node.context_anchor_id ?? node.output_context_anchor_id;
    if (!anchorId) return;
    document.getElementById(`trace-context-${anchorId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  return (
    <section className="content-panel panel-surface trace-panel">
      <header className="panel-heading">
        <div>
          <span className="section-kicker">Execution Trace</span>
          <h2>Agent 执行追踪</h2>
        </div>
        <div className="trace-heading-actions">
          <span className={`trace-status ${activeRun?.status ?? "running"}`}>{activeRun ? statusLabel(activeRun.status) : "等待 Trace"}</span>
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

      <div className="trace-panel-body trace-layout">
        {!hasActiveThread ? (
          <div className="empty-state trace-empty-state">
            <span className="card-line" />
            <h3>先选择一个会话</h3>
            <p>Trace 记录按会话归档，选择会话后才能查看执行历史。</p>
          </div>
        ) : null}

        {hasActiveThread ? (
          <>
            <TraceRunList runs={runs} activeTraceId={activeTraceId} onSelectTrace={onSelectTrace} />
            <TraceNodeTree nodes={nodes} activeNodeId={activeNodeId} loading={loading} hasActiveTrace={Boolean(activeTraceId)} onSelectNode={selectNode} />
            <TraceContextTimeline context={context} nodes={nodes} activeNode={activeNode} loading={loading} />
          </>
        ) : null}
      </div>
    </section>
  );
}

function TraceRunList({ runs, activeTraceId, onSelectTrace }: { runs: TraceRunSummary[]; activeTraceId: string; onSelectTrace: (traceId: string) => void }) {
  return (
    <aside className="trace-run-list" aria-label="Trace runs">
      {runs.length === 0 ? <p className="status-copy">当前会话还没有 Trace 记录。</p> : null}
      {runs.map((run) => (
        <button className={`trace-run-item ${run.trace_id === activeTraceId ? "active" : ""}`} type="button" key={run.trace_id} onClick={() => onSelectTrace(run.trace_id)}>
          <strong>{run.endpoint}</strong>
          <span>{formatDate(run.started_at)}</span>
          <small>
            {statusLabel(run.status)} · {run.event_count} events{run.duration_ms != null ? ` · ${formatDuration(run.duration_ms)}` : ""}
          </small>
        </button>
      ))}
    </aside>
  );
}

function TraceNodeTree({ nodes, activeNodeId, loading, hasActiveTrace, onSelectNode }: { nodes: TraceNode[]; activeNodeId: string; loading: boolean; hasActiveTrace: boolean; onSelectNode: (node: TraceNode) => void }) {
  return (
    <aside className="trace-node-tree" aria-label="Trace nodes">
      <div className="trace-column-heading">调用节点</div>
      {loading ? <p className="status-copy">正在加载 Trace...</p> : null}
      {!loading && hasActiveTrace && nodes.length === 0 ? <p className="status-copy">该 Trace 暂无语义节点。</p> : null}
      {!loading && !hasActiveTrace ? <p className="status-copy">选择一次 Trace 查看详情。</p> : null}
      {nodes.map((node) => (
        <button
          className={traceNodeClassName(node, node.node_id === activeNodeId)}
          type="button"
          key={node.node_id}
          style={{ paddingLeft: 12 + node.depth * 18 }}
          onClick={() => onSelectNode(node)}
        >
          <div className="trace-node-topline">
            <span className={nodeKindBadgeClassName(node)}>{nodeKindLabel(node)}</span>
            <span className="trace-node-metrics" aria-hidden="true" />
            <span className="trace-agent-badge">{nodeBadgeLabel(node)}</span>
          </div>
          <strong>{nodeBodyLabel(node)}</strong>
        </button>
      ))}
    </aside>
  );
}

function TraceContextTimeline({ context, nodes, activeNode, loading }: { context: TraceContextSegment[]; nodes: TraceNode[]; activeNode: TraceNode | null; loading: boolean }) {
  const nodesById = useMemo(() => new Map(nodes.map((node) => [node.node_id, node])), [nodes]);

  return (
    <div className="trace-context-panel">
      <div className="trace-column-heading">执行上下文</div>
      {loading ? <p className="status-copy">正在加载上下文...</p> : null}
      {!loading && context.length === 0 ? <p className="status-copy">上下文会在 Trace 详情加载后显示。</p> : null}
      <div className="trace-context-timeline" aria-live="polite">
        {context.map((segment) => (
          <TraceContextSegmentItem
            segment={segment}
            node={traceNodeForSegment(segment, nodes, nodesById)}
            key={segment.anchor_id}
            active={isActiveSegment(segment, activeNode)}
          />
        ))}
      </div>
    </div>
  );
}

function TraceContextSegmentItem({ segment, node, active }: { segment: TraceContextSegment; node: TraceNode | null; active: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const [metadataExpanded, setMetadataExpanded] = useState(false);
  const phase = traceSegmentPhase(segment);
  const canCollapse = segment.kind === "tool" || segment.kind === "human" || segment.kind === "ai" || segment.kind === "system";
  const text = typeof segment.content === "string" ? segment.content : JSON.stringify(segment.content, null, 2) ?? "";
  const shouldCollapse = canCollapse && countLines(text) > 6;
  const body = <TraceContent value={segment.content} kind={segment.kind} collapsed={shouldCollapse && !expanded} />;
  const hasMetadata = Boolean(segment.metadata && Object.keys(segment.metadata).length > 0);

  return (
    <article id={`trace-context-${segment.anchor_id}`} className={`trace-context-segment ${safeClassName(segment.kind)} phase-${phase} ${active ? "active" : ""}`} style={{ marginLeft: segment.depth * 16 }}>
      <header className="trace-context-header">
        <span className="trace-context-heading">
          <span className={contextKindBadgeClassName(node, segment.kind)}>{contextKindLabel(node, segment)}</span>
          <span className={`trace-phase-badge ${phase}`}>{phaseLabel(phase)}</span>
        </span>
        <TraceNodeMetricTags node={node} />
        <span className={`trace-agent-badge ${segment.agent_role === "subagent" ? "subagent" : "main-agent"}`}>{contextBadgeLabel(segment)}</span>
      </header>
      <h3>{segment.title}</h3>
      {segment.collapsed_by_default ? (
        <details>
          <summary>展开内部上下文</summary>
          {body}
        </details>
      ) : (
        body
      )}
      <ToolCallTags names={segment.tool_call_names} />
      {shouldCollapse || hasMetadata ? (
        <div className="trace-segment-actions">
          {shouldCollapse ? (
            <button className="trace-content-toggle trace-expand-toggle" type="button" onClick={() => setExpanded((current) => !current)}>
              <span>{expanded ? "收起内容" : "展开全部"}</span>
            </button>
          ) : null}
          {hasMetadata ? (
            <>
              <button className="trace-content-toggle" type="button" onClick={() => setMetadataExpanded((current) => !current)}>
                <span>{metadataExpanded ? "收起元数据" : "查看元数据"}</span>
              </button>
              {metadataExpanded ? (
                <div className="trace-raw-details trace-metadata-details">
                  <pre>{JSON.stringify(segment.metadata, null, 2)}</pre>
                </div>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

function TraceContent({ value, kind, collapsed = false }: { value: unknown; kind: TraceContextSegment["kind"]; collapsed?: boolean }) {
  if (kind === "todo" && Array.isArray(value)) {
    return <TodoList value={value} />;
  }
  if (kind === "tool" || kind === "human" || kind === "ai") {
    return <CollapsibleTraceContent value={value} collapsed={collapsed} />;
  }
  if (typeof value === "string") {
    return <p className="trace-context-copy">{value || "空内容"}</p>;
  }
  return <pre className="trace-context-json">{JSON.stringify(value, null, 2)}</pre>;
}

function ToolCallTags({ names }: { names: string[] }) {
  if (names.length === 0) {
    return null;
  }

  return (
    <div className="trace-tool-call-tags" aria-label="调用工具">
      {names.map((name, index) => (
        <span className="trace-tool-call-tag" key={`${name}-${index}`}>
          {name}
        </span>
      ))}
    </div>
  );
}

function CollapsibleTraceContent({ value, collapsed }: { value: unknown; collapsed: boolean }) {
  const isString = typeof value === "string";
  const text = isString ? value : JSON.stringify(value, null, 2) ?? "";
  const className = `trace-collapsible-content ${collapsed ? "collapsed" : ""}`;

  return <div className={className}>{isString ? <p className="trace-context-copy">{text}</p> : <pre className="trace-context-json">{text}</pre>}</div>;
}

function countLines(value: string) {
  return value.split(/\r\n|\r|\n/).length;
}

function TodoList({ value }: { value: unknown[] }) {
  const items = value as TraceTodoItem[];
  return (
    <ul className="trace-todo-list">
      {items.map((item, index) => (
        <li className={`trace-todo-item ${item.status}`} key={item.id ?? `${item.content}-${index}`}>
          <span>{todoStatusLabel(item.status)}</span>
          <p>{item.content}</p>
        </li>
      ))}
    </ul>
  );
}

function TraceNodeMetricTags({ node }: { node: TraceNode | null }) {
  const metrics = node ? traceNodeMetrics(node) : [];

  return (
    <span className="trace-node-metrics">
      {metrics.map((metric) => (
        <span className={`trace-node-metric ${metric.tone ? safeClassName(metric.tone) : ""}`} key={metric.key}>
          {metric.label}
        </span>
      ))}
    </span>
  );
}

function traceNodeMetrics(node: TraceNode): TraceMetricItem[] {
  const metrics: TraceMetricItem[] = [
    { key: "status", label: statusLabel(node.status), tone: `status-${node.status}` },
    { key: "duration", label: node.duration_ms != null ? formatDuration(node.duration_ms) : "耗时 --", tone: "duration" },
  ];
  if (node.usage?.input_tokens != null) metrics.push({ key: "input", label: `输入 ${node.usage.input_tokens}`, tone: "tokens" });
  if (node.usage?.output_tokens != null) metrics.push({ key: "output", label: `输出 ${node.usage.output_tokens}`, tone: "tokens" });
  return metrics;
}

function contextKindLabel(node: TraceNode | null, segment: TraceContextSegment) {
  if (node?.kind === "llm") return "LLM";
  if (node?.kind === "tool") return "Tool";
  return SEGMENT_LABELS[segment.kind] ?? formatTraceLabel(segment.kind);
}

function contextKindBadgeClassName(node: TraceNode | null, segmentKind: string) {
  const kind = node?.kind === "llm" || node?.kind === "tool" ? node.kind : segmentKind;
  return `trace-context-kind-badge ${safeClassName(kind)}`;
}

function traceNodeForSegment(segment: TraceContextSegment, nodes: TraceNode[], nodesById: Map<string, TraceNode>) {
  if (segment.related_node_id) return nodesById.get(segment.related_node_id) ?? null;
  return nodes.find((node) => node.input_context_range?.start_anchor_id === segment.anchor_id || node.input_context_range?.end_anchor_id === segment.anchor_id || node.context_anchor_id === segment.anchor_id || node.output_context_anchor_id === segment.anchor_id) ?? null;
}

function isActiveSegment(segment: TraceContextSegment, activeNode: TraceNode | null) {
  if (!activeNode) return false;
  return (
    segment.related_node_id === activeNode.node_id ||
    segment.anchor_id === activeNode.context_anchor_id ||
    segment.anchor_id === activeNode.output_context_anchor_id ||
    segment.anchor_id === activeNode.input_context_range?.start_anchor_id ||
    segment.anchor_id === activeNode.input_context_range?.end_anchor_id
  );
}

function traceSegmentPhase(segment: TraceContextSegment): TraceSegmentPhase {
  const phase = segment.metadata?.phase;
  if (phase === "input" || phase === "output" || phase === "error") return phase;
  if (segment.kind === "error") return "error";
  return "context";
}

function phaseLabel(phase: TraceSegmentPhase) {
  if (phase === "input") return "输入";
  if (phase === "output") return "输出";
  if (phase === "error") return "错误";
  return "上下文";
}

function contextBadgeLabel(segment: TraceContextSegment) {
  if (segment.agent_name) {
    const role = segment.agent_role === "subagent" ? "Subagent" : "Main";
    return `${role} · ${compactAgentName(segment.agent_name)}`;
  }
  return SEGMENT_LABELS[segment.kind] ?? formatTraceLabel(segment.kind);
}

function traceNodeClassName(node: TraceNode, active: boolean) {
  const roleClass = node.agent_role === "subagent" ? "subagent" : node.agent_role ? safeClassName(node.agent_role) : "main-agent";
  return ["trace-node-item", active ? "active" : "", roleClass, node.status].filter(Boolean).join(" ");
}

function nodeKindBadgeClassName(node: TraceNode) {
  return `trace-node-kind ${safeClassName(node.kind)}`;
}

function nodeKindLabel(node: TraceNode) {
  return NODE_LABELS[node.kind] ?? formatTraceLabel(node.kind);
}

function nodeBadgeLabel(node: TraceNode) {
  if (node.agent_name) return compactAgentName(node.agent_name);
  return nodeKindLabel(node);
}

function nodeBodyLabel(node: TraceNode) {
  return node.model_name || node.tool_name || node.label || nodeKindLabel(node);
}

function formatTraceLabel(value: string) {
  return value
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ") || value;
}

function safeClassName(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9_-]/g, "-");
}

function statusLabel(status: TraceRunSummary["status"]) {
  if (status === "completed") return "完成";
  if (status === "failed") return "失败";
  return "运行中";
}

function todoStatusLabel(status: TraceTodoItem["status"]) {
  if (status === "completed") return "完成";
  if (status === "in_progress") return "进行中";
  return "待办";
}

function compactAgentName(value: string) {
  return value.replace(/-subagent$/, "").replace(/-agent$/, "");
}

function formatDate(value: string) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

function formatDuration(value: number) {
  if (value < 1000) return `${value}ms`;
  return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}s`;
}
