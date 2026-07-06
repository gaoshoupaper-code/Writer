"use client";

/**
 * 要素 Tab 容器（右侧主区域顶部）。
 *
 * 四个 Tab：Prompt / Middleware / Skills / Subagents。
 * Tab 内按 Agent（meta 在前）纵向排卡片。
 * Tab 选择用组件 state（不进 URL，D6）。
 *
 * - Prompt Tab：每个 Agent 一张卡，展示 prompt 全文（Markdown 渲染）
 * - Middleware Tab：每个 Agent 一张卡，列出 middleware 元信息，源码懒加载折叠
 * - Skills Tab：每个 Agent 一张卡，展示每个 skill 的 SKILL.md 全文
 * - Subagents Tab：编排关系图（meta → 5 subagent）
 */
import type { ElementsView } from "@/lib/harness-types";
import { AgentCard } from "./AgentCard";
import { SubagentGraph } from "./SubagentGraph";

export type ElementTab = "prompt" | "middleware" | "skills" | "subagents";

const TABS: { key: ElementTab; label: string }[] = [
  { key: "prompt", label: "Prompt" },
  { key: "middleware", label: "Middleware" },
  { key: "skills", label: "Skills" },
  { key: "subagents", label: "Subagents" },
];

interface Props {
  view: ElementsView;
  version: number;
  tab: ElementTab;
  onTabChange: (tab: ElementTab) => void;
}

export function ElementTabs({ view, version, tab, onTabChange }: Props) {
  return (
    <div className="harness-elements card" style={{ padding: 0 }}>
      {/* 版本头 */}
      <div className="harness-elements-head">
        <div>
          <span className="mono" style={{ fontSize: 16, fontWeight: 700 }}>v{version}</span>
          <span className="text-dim" style={{ marginLeft: 12, fontSize: 12 }}>
            {view.source_commit ? `commit ${view.source_commit}` : "无 source_commit"}
          </span>
        </div>
        {!view.has_source && (
          <span className="harness-no-source-tag">该版本无源码记录</span>
        )}
      </div>

      {/* Tab 栏 */}
      <div className="harness-tab-bar">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`harness-tab ${tab === t.key ? "active" : ""}`}
            onClick={() => onTabChange(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab 内容 */}
      <div className="harness-tab-content">
        {tab === "subagents" ? (
          <SubagentGraph view={view} />
        ) : (
          <div className="harness-agent-list">
            {view.agents.map((agent) => (
              <AgentCard
                key={agent.name}
                agent={agent}
                tab={tab}
                version={version}
                hasSource={view.has_source}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
