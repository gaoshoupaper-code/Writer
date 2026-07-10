/**
 * Trace node patch 处理（Phase 2 T4 路线 Y + T6）。
 *
 * 设计变更：前端不再做投影（projectTraceDetail ~800 行已删除）。
 * 投影完全由后端负责，SSE 推送 node patch（append/update），前端只做轻量的数组操作。
 * 这消除了旧 O(N²) 瓶颈（每条 SSE 事件全量 filter+sort+重投影）。
 */

import type { TraceDetailLite, TraceNode, NodePatch } from "./types";

/**
 * 应用 node patch 到当前 detail（append 新 node / update 变更 node）。
 *
 * evolution 源 SSE 推送 NodePatch，前端调用此函数更新 detail.nodes。
 * 复杂度 O(A+U)（A=append 数，U=update 数），不再依赖 nodes 总量。
 */
export function applyNodePatch(
  current: TraceDetailLite | null,
  patch: NodePatch,
): TraceDetailLite | null {
  if (!current) return current;

  // update：按 node_id 覆盖已有 node（全量替换，Phase 2 T6）
  if (patch.updated.length > 0) {
    const updateMap = new Map(patch.updated.map((n) => [n.node_id, n]));
    current = {
      ...current,
      nodes: current.nodes.map((n) => updateMap.get(n.node_id) ?? n),
    };
  }

  // append：追加新 node（保持顺序）
  if (patch.appended.length > 0) {
    current = {
      ...current,
      nodes: [...current.nodes, ...patch.appended],
    };
  }

  return current;
}

/**
 * 用全量 nodes 替换当前 detail（终态 snapshot 用，Phase 2 T9）。
 *
 * 终态时后端推全量 snapshot，前端用它强制对齐——无论运行期丢了什么 patch，
 * 终态 snapshot 一发就纠正。
 */
export function applyNodeSnapshot(
  current: TraceDetailLite | null,
  nodes: TraceNode[],
): TraceDetailLite | null {
  if (!current) return current;
  return { ...current, nodes };
}
