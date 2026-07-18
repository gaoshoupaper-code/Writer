import { useState } from "react";
import type { HarnessElementView, AgentDiff, Hunk } from "@/lib/api";
import { AgentBadge } from "./AgentBadge";
import { PromptDiffViewer } from "./PromptDiffViewer";

/**
 * Prompt Tab：按 agent 分段展示各 agent 的系统提示词。
 *
 * - 默认显示当前版本全文（D6）
 * - Tab 内独立"显示 diff"开关（D18），开启后对该 agent 有 prompt diff 的段落叠加行级高亮（D15）
 * - 每段标 Agent 徽章（D5）
 */
export function PromptTab({
  agents,
  diffs,
}: {
  agents: HarnessElementView[];
  diffs: Map<string, AgentDiff> | null;
}) {
  const [showDiff, setShowDiff] = useState(false);

  // 无 diff 数据时禁用开关（首版本 / 无变化版本）
  const hasAnyPromptDiff = diffs
    ? agents.some((a) => diffs.get(a.name)?.prompt)
    : false;

  return (
    <div>
      <div className="diff-toggle-bar">
        <div
          className={`diff-toggle ${showDiff && hasAnyPromptDiff ? "on" : ""}`}
          onClick={() => hasAnyPromptDiff && setShowDiff(!showDiff)}
          role="switch"
          aria-checked={showDiff && hasAnyPromptDiff}
          style={!hasAnyPromptDiff ? { opacity: 0.4, cursor: "not-allowed" } : undefined}
        />
        <span>显示升级差异{!hasAnyPromptDiff && "（本版本无 prompt 变更）"}</span>
      </div>

      {agents.map((agent) => {
        const promptDiff = diffs?.get(agent.name)?.prompt ?? null;
        return (
          <div key={agent.name} className="element-agent-group">
            <div className="element-agent-group-head">
              <AgentBadge name={agent.name} />
              <h4>系统提示词</h4>
              {showDiff && promptDiff && (
                <span className="upgrade-change-desc" style={{ fontSize: 11 }}>
                  <span className="diff-tag-add">+{promptDiff.summary.added}</span>
                  {" / "}
                  <span className="diff-tag-del">-{promptDiff.summary.removed}</span>
                  {" 行"}
                </span>
              )}
            </div>
            <PromptBody
              body={agent.prompt.body}
              hunks={promptDiff?.hunks ?? null}
              showDiff={showDiff}
            />
          </div>
        );
      })}
    </div>
  );
}

/** 单个 agent 的 prompt 渲染：全文或 diff 高亮 */
function PromptBody({
  body,
  hunks,
  showDiff,
}: {
  body: string;
  hunks: Hunk[] | null;
  showDiff: boolean;
}) {
  if (!body) return <p className="prompt-empty">（无 prompt）</p>;

  // diff 模式且该 agent 有 prompt diff → 行级高亮；否则全文
  if (showDiff && hunks) {
    return <PromptDiffViewer hunks={hunks} />;
  }
  return <pre className="prompt-full">{body}</pre>;
}
