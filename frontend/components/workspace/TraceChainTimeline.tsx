import { Fragment, forwardRef, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { TraceNode, TraceRunSummary } from "../../lib/types";
import { ChainIcon } from "./ChainNodeIcon";

/**
 * 链路时间线 — 显示 trace 执行节点的时间线视图。
 * - Agent 节点可折叠/展开其子节点
 * - 每个节点显示 SVG 图标 + 名称 + chain_summary + 耗时 + 状态
 * - 点击可展开的节点（llm/todo/error）打开右侧抽屉
 * - LLM 节点支持跳转到图检测
 */

type TraceChainTimelineProps = {
  nodes: TraceNode[];
  activeRun: TraceRunSummary | null;
  activeNodeId: string;
  onSelectNode: (node: TraceNode) => void;
  /** 从执行追踪跳转到图检测：传入 LLM 节点对应的 loopIndex */
  onJumpToChart?: (loopIndex: number) => void;
  /** 高亮指定的节点（从图检测跳转过来时使用） */
  highlightedNodeId: string | null;
  /** 高亮结束后清除 */
  onClearHighlight?: () => void;
};

/** 判断节点是否可打开抽屉 */
function isExpandable(node: TraceNode): boolean {
  return node.kind === "llm" || node.kind === "todo" || node.kind === "error";
}

type RenderItem =
  | { type: "single"; node: TraceNode }
  | { type: "parallel"; groupId: string; nodes: TraceNode[] };

export function TraceChainTimeline({ nodes, activeRun, activeNodeId, onSelectNode, onJumpToChart, highlightedNodeId, onClearHighlight }: TraceChainTimelineProps) {
  const [collapsedAgents, setCollapsedAgents] = useState<Set<string>>(new Set());
  const nodeRowRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  // ── 高亮效果：从图检测跳转过来时，展开父 agent、滚动到目标位置并高亮 ──
  useEffect(() => {
    if (!highlightedNodeId) return;
    // 找到目标节点
    const targetNode = nodes.find((n) => n.node_id === highlightedNodeId);
    if (!targetNode) return;
    // 确保父 agent 未折叠
    if (targetNode.parent_node_id && collapsedAgents.has(targetNode.parent_node_id)) {
      setCollapsedAgents((prev) => {
        const next = new Set(prev);
        next.delete(targetNode.parent_node_id!);
        return next;
      });
    }
    // 滚动到目标（使用 requestAnimationFrame 等待 DOM 更新）
    requestAnimationFrame(() => {
      const el = nodeRowRefs.current.get(highlightedNodeId);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
    // 3 秒后自动清除高亮
    const timer = setTimeout(() => {
      onClearHighlight?.();
    }, 3000);
    return () => clearTimeout(timer);
  }, [highlightedNodeId, nodes]);

  // 过滤掉 run 节点
  const chainNodes = nodes.filter((n) => n.kind !== "run");

  // 收集 agent 节点 id → 用于判断折叠
  const agentNodeIds = useMemo(
    () => new Set(chainNodes.filter((n) => n.kind === "agent").map((n) => n.node_id)),
    [chainNodes],
  );

  // 根据折叠状态过滤可见节点
  const visibleNodes = useMemo(() => {
    const hidden = new Set<string>();
    for (const node of chainNodes) {
      // 如果该节点的父 agent 被折叠，隐藏它
      if (node.parent_node_id && agentNodeIds.has(node.parent_node_id) && collapsedAgents.has(node.parent_node_id)) {
        hidden.add(node.node_id);
      }
    }
    return chainNodes.filter((n) => !hidden.has(n.node_id));
  }, [chainNodes, agentNodeIds, collapsedAgents]);

  function toggleAgent(agentNodeId: string) {
    setCollapsedAgents((prev) => {
      const next = new Set(prev);
      if (next.has(agentNodeId)) {
        next.delete(agentNodeId);
      } else {
        next.add(agentNodeId);
      }
      return next;
    });
  }

  // 统计每个 agent 的子节点数（用于折叠指示器）
  const agentChildCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const node of chainNodes) {
      if (node.parent_node_id && agentNodeIds.has(node.parent_node_id)) {
        counts.set(node.parent_node_id, (counts.get(node.parent_node_id) ?? 0) + 1);
      }
    }
    return counts;
  }, [chainNodes, agentNodeIds]);

  // 并行组映射：groupId → 同组节点列表（仅 depth=0，符合设计 D4）
  const parallelGroups = useMemo(() => {
    const groups = new Map<string, TraceNode[]>();
    for (const node of chainNodes) {
      if (node.parallel_group_id && node.depth === 0) {
        if (!groups.has(node.parallel_group_id)) {
          groups.set(node.parallel_group_id, []);
        }
        groups.get(node.parallel_group_id)!.push(node);
      }
    }
    return groups;
  }, [chainNodes]);

  // 拉取并行节点到首次出现位置，构建渲染列表
  const renderItems = useMemo((): RenderItem[] => {
    const items: RenderItem[] = [];
    const consumed = new Set<string>();
    for (const node of visibleNodes) {
      if (consumed.has(node.node_id)) continue;
      // 仅 depth=0 的并行组做横向分组（D4）
      if (node.parallel_group_id && node.depth === 0) {
        const group = parallelGroups.get(node.parallel_group_id);
        if (group && group.length > 1) {
          items.push({ type: "parallel", groupId: node.parallel_group_id, nodes: group });
          group.forEach((n) => consumed.add(n.node_id));
          continue;
        }
      }
      items.push({ type: "single", node });
      consumed.add(node.node_id);
    }
    return items;
  }, [visibleNodes, parallelGroups]);

  // LLM 节点 → loopIndex 映射（用于追踪→图跳转）
  // loopIndex = 该 LLM 节点在所有 LLM 节点（含并行对齐）中的序号
  const llmLoopIndexMap = useMemo(() => {
    const map = new Map<string, number>();
    let idx = 0;
    const groupIndexMap = new Map<string, number>();
    for (const node of chainNodes) {
      if (node.kind !== "llm") continue;
      if (node.parallel_group_id) {
        const existing = groupIndexMap.get(node.parallel_group_id);
        if (existing !== undefined) {
          map.set(node.node_id, existing);
          continue;
        }
      }
      idx++;
      if (node.parallel_group_id) {
        groupIndexMap.set(node.parallel_group_id, idx);
      }
      map.set(node.node_id, idx);
    }
    return map;
  }, [chainNodes]);

  const setNodeRowRef = useCallback((nodeId: string, el: HTMLDivElement | null) => {
    if (el) {
      nodeRowRefs.current.set(nodeId, el);
    } else {
      nodeRowRefs.current.delete(nodeId);
    }
  }, []);

  return (
    <div className="trace-chain-timeline">
      {/* 状态摘要条 */}
      <div className="trace-chain-status-bar">
        <ChainStatusBar nodes={nodes} activeRun={activeRun} />
      </div>

      {/* 节点列表 */}
      <div className="trace-chain-nodes">
        {chainNodes.length === 0 ? (
          <p className="status-copy">暂无执行节点。</p>
        ) : null}
        {renderItems.map((item) => {
          if (item.type === "parallel") {
            return (
              <div key={item.groupId} className="chain-parallel-group">
                {item.nodes.map((node, i) => (
                  <Fragment key={node.node_id}>
                    {i > 0 && <div className="chain-parallel-divider" />}
                    <ChainNodeRow
                      ref={(el) => setNodeRowRef(node.node_id, el)}
                      node={node}
                      active={node.node_id === activeNodeId}
                      highlighted={node.node_id === highlightedNodeId}
                      expandable={isExpandable(node)}
                      isAgent={node.kind === "agent"}
                      isCollapsed={node.kind === "agent" && collapsedAgents.has(node.node_id)}
                      childCount={node.kind === "agent" ? (agentChildCounts.get(node.node_id) ?? 0) : 0}
                      loopIndex={llmLoopIndexMap.get(node.node_id)}
                      onSelect={onSelectNode}
                      onToggleAgent={toggleAgent}
                      onJumpToChart={onJumpToChart}
                    />
                  </Fragment>
                ))}
              </div>
            );
          }
          const node = item.node;
          return (
            <ChainNodeRow
              key={node.node_id}
              ref={(el) => setNodeRowRef(node.node_id, el)}
              node={node}
              active={node.node_id === activeNodeId}
              highlighted={node.node_id === highlightedNodeId}
              expandable={isExpandable(node)}
              isAgent={node.kind === "agent"}
              isCollapsed={node.kind === "agent" && collapsedAgents.has(node.node_id)}
              childCount={node.kind === "agent" ? (agentChildCounts.get(node.node_id) ?? 0) : 0}
              loopIndex={llmLoopIndexMap.get(node.node_id)}
              onSelect={onSelectNode}
              onToggleAgent={toggleAgent}
              onJumpToChart={onJumpToChart}
            />
          );
        })}
      </div>
    </div>
  );
}

/** 状态摘要条 */
function ChainStatusBar({ nodes, activeRun }: { nodes: TraceNode[]; activeRun: TraceRunSummary | null }) {
  const total = nodes.filter((n) => n.kind !== "run").length;
  const errors = nodes.filter((n) => n.kind === "error" || n.status === "failed").length;
  const running = nodes.filter((n) => n.status === "running").length;

  return (
    <div className="chain-status-bar-inner">
      <span className="chain-status-metric">
        <span className="chain-status-label">节点</span>
        <strong>{total}</strong>
      </span>
      {errors > 0 ? (
        <span className="chain-status-metric error">
          <span className="chain-status-label">错误</span>
          <strong>{errors}</strong>
        </span>
      ) : null}
      {running > 0 ? (
        <span className="chain-status-metric running">
          <span className="chain-status-label">运行中</span>
          <strong>{running}</strong>
        </span>
      ) : null}
      {activeRun?.duration_ms != null ? (
        <span className="chain-status-metric duration">
          <span className="chain-status-label">总耗时</span>
          <strong>{formatDuration(activeRun.duration_ms)}</strong>
        </span>
      ) : null}
      {activeRun ? (
        <span className={`chain-status-run-status ${activeRun.status}`}>
          {statusLabel(activeRun.status)}
        </span>
      ) : null}
    </div>
  );
}

/** 链路节点行 */
const ChainNodeRow = forwardRef<HTMLDivElement, {
  node: TraceNode;
  active: boolean;
  highlighted: boolean;
  expandable: boolean;
  isAgent: boolean;
  isCollapsed: boolean;
  childCount: number;
  /** LLM 节点对应的 loopIndex（用于跳转到图检测） */
  loopIndex?: number;
  onSelect: (node: TraceNode) => void;
  onToggleAgent: (agentNodeId: string) => void;
  onJumpToChart?: (loopIndex: number) => void;
}>(function ChainNodeRow({
  node,
  active,
  highlighted,
  expandable,
  isAgent,
  isCollapsed,
  childCount,
  loopIndex,
  onSelect,
  onToggleAgent,
  onJumpToChart,
}, ref) {
  const handleClick = () => {
    if (isAgent) {
      onToggleAgent(node.node_id);
    } else {
      onSelect(node);
    }
  };

  // 仅 LLM 节点可跳转到图检测
  const canJumpToChart = node.kind === "llm" && loopIndex != null && onJumpToChart;

  // depth-based 缩进：depth=1 → 28px，depth=2（evaluation）→ 56px
  const indent = node.depth && node.depth > 0 ? 28 * node.depth : 0;

  return (
    <div
      ref={ref}
      className={`chain-node-row-wrapper ${node.depth && node.depth > 0 ? "subagent-indent" : ""} ${node.depth && node.depth >= 2 ? "eval-indent" : ""} ${highlighted ? "chain-node-highlighted" : ""}`}
      style={{ paddingLeft: indent }}
    >
      <button
        className={`chain-node-row ${node.kind} ${node.status} ${active ? "active" : ""} ${highlighted ? "highlighted" : ""} ${expandable ? "expandable" : ""} ${node.kind === "error" || node.status === "failed" ? "failed" : ""}`}
        type="button"
        onClick={handleClick}
      >
        <div className="chain-node-header">
          <span className={`chain-node-icon ${node.kind}`}>
            <ChainIcon kind={node.kind} size={14} />
          </span>
          <span className="chain-node-label">{nodeBodyLabel(node)}</span>
          {isAgent && childCount > 0 ? (
            <span className={`chain-collapse-badge ${isCollapsed ? "collapsed" : ""}`}>
              {isCollapsed ? `${childCount} ▸` : "▾"}
            </span>
          ) : null}
          <span className={`chain-node-duration ${durationColorClass(node.duration_ms)}`}>{node.duration_ms != null ? formatDuration(node.duration_ms) : ""}</span>
          <span className={`chain-node-status-dot ${node.status}`} />
        </div>
        {node.chain_summary ? (
          <div className="chain-node-body">
            <span className="chain-node-summary">{node.chain_summary}</span>
          </div>
        ) : null}
      </button>
      {canJumpToChart ? (
        <button
          className="chain-node-jump-chart-btn"
          type="button"
          title="在图检测中查看"
          onClick={(e) => {
            e.stopPropagation();
            onJumpToChart(loopIndex);
          }}
        >
          📊
        </button>
      ) : null}
    </div>
  );
});

function nodeBodyLabel(node: TraceNode): string {
  if (node.kind === "agent") {
    const name = node.agent_name || "Agent";
    // 从 node_id 提取实例序号（如 agent:writing-subagent:1 → #1）
    const match = node.node_id.match(/^agent:.+:(\d+)$/);
    if (match) {
      const display = name.replace(/-subagent$/, "");
      return `${display} #${match[1]}`;
    }
    return name;
  }
  if (node.kind === "llm") return node.model_name || "LLM";
  if (node.kind === "tool") return node.tool_name || "Tool";
  if (node.kind === "todo") return "Todo 更新";
  if (node.kind === "error") return "错误";
  return node.label || node.kind;
}

/** 从 raw_event_ids 中提取事件序号范围，如 ["trace-xx-4","trace-xx-6"] → "#4-6" */
function eventNumberLabel(rawEventIds?: string[]): string | null {
  if (!rawEventIds || rawEventIds.length === 0) return null;
  const seqs = rawEventIds
    .map((id) => {
      const parts = id.split("-");
      const last = parts[parts.length - 1];
      const n = parseInt(last, 10);
      return Number.isNaN(n) ? null : n;
    })
    .filter((n): n is number => n !== null)
    .sort((a, b) => a - b);
  if (seqs.length === 0) return null;
  const first = seqs[0];
  const last = seqs[seqs.length - 1];
  return first === last ? `#${first}` : `#${first}-${last}`;
}

function statusLabel(status: string): string {
  if (status === "completed") return "完成";
  if (status === "failed") return "失败";
  return "运行中";
}

function formatDuration(value: number): string {
  if (value < 1000) return `${value}ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}s`;
  if (value < 3_600_000) return `${(value / 60_000).toFixed(value < 600_000 ? 1 : 0)}min`;
  return `${(value / 3_600_000).toFixed(1)}h`;
}

/** 根据节点耗时返回颜色类名：<20s 绿 · <60s 黄 · ≥60s 红 */
function durationColorClass(ms: number | null | undefined): string {
  if (ms == null) return "";
  if (ms < 20_000) return "duration-fast";
  if (ms < 60_000) return "duration-medium";
  return "duration-slow";
}
