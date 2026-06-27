"use client";

/**
 * AdaptPipeline —— 9 节点流水线可视化（驾驶舱左栏，D4）。
 *
 * adapt loop 是固定流程 graph，把它画成一条横向流水线：
 *   run_baseline → planner → evolver → run_candidates → evaluate → critic
 *                                                     ↑ revision 回环 ↓
 *   gate → ship → loop_control
 *
 * 状态映射：
 *   done（已产出）= 实色 accent
 *   active（当前节点）= accent + 脉冲圈
 *   pending（未到）= 暗色
 *
 * revision 回环（critic→evolver）单独用一条弧线表示，触发时高亮。
 */
import type { AdaptNodeName } from "@/lib/adapt-types";

// 节点定义：主线顺序 + 中文名 + 是否可被 revision 回访
const NODES: { id: AdaptNodeName; label: string }[] = [
  { id: "run_baseline", label: "基准" },
  { id: "planner", label: "规划" },
  { id: "evolver", label: "演化" },
  { id: "run_candidates", label: "跑候选" },
  { id: "evaluate", label: "评估" },
  { id: "critic", label: "审视" },
  { id: "gate", label: "门控" },
  { id: "ship", label: "发布" },
  { id: "loop_control", label: "循环" },
];

// revision 回环：critic → evolver
const REVISION_FROM = "critic";
const REVISION_TO = "evolver";

type NodeState = "pending" | "active" | "done";

export function AdaptPipeline({
  current,
  completed,
  round,
  revisionActive,
}: {
  /** 当前正在执行的节点（来自最新 node_output） */
  current: AdaptNodeName | null;
  /** 已完成的节点集合（本 session 跑过的） */
  completed: Set<AdaptNodeName>;
  round: number;
  revisionActive: boolean;
}) {
  const stateOf = (id: AdaptNodeName): NodeState => {
    if (id === current) return "active";
    if (completed.has(id)) return "done";
    return "pending";
  };

  return (
    <div className="pipeline">
      <div className="pipeline-round mono">
        ROUND {round}
        {revisionActive && (
          <span className="pipeline-revision-tag">REVISION</span>
        )}
      </div>
      <div className="pipeline-track">
        {NODES.map((n, i) => {
          const st = stateOf(n.id);
          return (
            <div key={n.id} className="pipeline-node-wrap">
              <div
                className={`pipeline-node pipeline-node-${st}`}
                title={n.id}
              >
                <span className="pipeline-node-dot" />
                <span className="pipeline-node-label">{n.label}</span>
              </div>
              {i < NODES.length - 1 && (
                <div
                  className={`pipeline-connector pipeline-connector-${
                    st === "done" ? "done" : "pending"
                  }`}
                />
              )}
            </div>
          );
        })}
      </div>
      {/* revision 回环弧线指示 */}
      <div className={`pipeline-revision ${revisionActive ? "active" : ""}`}>
        <span className="pipeline-revision-label mono">
          critic → evolver（修订回环）
        </span>
      </div>
    </div>
  );
}
