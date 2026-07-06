"use client";

/**
 * Subagents Tab：meta → 5 subagent 编排关系图。
 *
 * 纯 CSS 连线图（不上 xyflow，D7）：
 *   - 顶部 meta 节点
 *   - 中间一条垂直干线 + 水平分支
 *   - 底部 5 个 subagent 节点横排，每个标注中文 role
 */
import type { ElementsView } from "@/lib/harness-types";

// subagent 机器名 → 中文角色名（与 AgentCard 对齐）
const ROLE_LABEL: Record<string, string> = {
  interview: "需求访谈",
  storybuilding: "故事构建",
  detail_outline: "细纲生成",
  writing: "正文写作",
  general_purpose: "通用助手",
};

interface Props {
  view: ElementsView;
}

export function SubagentGraph({ view }: Props) {
  const subagents = view.agents.filter((a) => a.kind === "subagent");

  return (
    <div className="harness-graph">
      <p className="text-dim harness-graph-intro">
        meta（总控）通过 task 工具委托以下 {subagents.length} 个子代理完成创作流水线。
      </p>

      <div className="harness-graph-canvas">
        {/* meta 节点 */}
        <div className="harness-graph-meta">
          <div className="harness-graph-node-meta">
            <span className="harness-graph-node-title">Meta（总控）</span>
            <span className="harness-graph-node-sub mono text-mute">
              只读 + 委托 · {view.agents[0]?.middlewares.length ?? 0} middleware
            </span>
          </div>
        </div>

        {/* 连线：垂直主干 */}
        <div className="harness-graph-trunk" />

        {/* 连线：水平分支（占位，视觉对齐用） */}
        <div className="harness-graph-branch" />

        {/* subagent 节点横排 */}
        <div className="harness-graph-subs">
          {subagents.map((sub) => {
            const role = ROLE_LABEL[sub.name] ?? sub.name;
            return (
              <div key={sub.name} className="harness-graph-sub-col">
                {/* 每个节点向上的短连接线 */}
                <div className="harness-graph-stem" />
                <div className="harness-graph-node-sub">
                  <span className="harness-graph-node-title">{role}</span>
                  <span className="harness-graph-node-sub-name mono text-mute">
                    {sub.name}
                  </span>
                  <span className="harness-graph-node-stats mono text-mute">
                    {sub.middlewares.length} mw · {sub.skills.length} skill
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
