import { useState } from "react";
import type { AgentElementView, AgentDiff } from "@/lib/api";
import { AgentBadge } from "./AgentBadge";

/**
 * Skills Tab：按 agent 分段展示各 agent 的技能路径列表。
 *
 * - 默认显示当前版本全量 skills 路径（D6）
 * - Tab 内独立"显示 diff"开关（D18），开启后：新增路径绿底、删除路径红底（D13）
 * - 删除的路径当前版本 elements 里不存在，从 diff 数据补出来渲染
 * - 每段标 Agent 徽章（D5）
 */
export function SkillsTab({
  agents,
  diffs,
}: {
  agents: AgentElementView[];
  diffs: Map<string, AgentDiff> | null;
}) {
  const [showDiff, setShowDiff] = useState(false);

  // 无 diff 数据时禁用开关
  const hasAnySkillsDiff = diffs
    ? agents.some((a) => diffs.get(a.name)?.skills)
    : false;

  return (
    <div>
      <div className="diff-toggle-bar">
        <div
          className={`diff-toggle ${showDiff && hasAnySkillsDiff ? "on" : ""}`}
          onClick={() => hasAnySkillsDiff && setShowDiff(!showDiff)}
          role="switch"
          aria-checked={showDiff && hasAnySkillsDiff}
          style={!hasAnySkillsDiff ? { opacity: 0.4, cursor: "not-allowed" } : undefined}
        />
        <span>显示升级差异{!hasAnySkillsDiff && "（本版本无 skills 变更）"}</span>
      </div>

      {agents.map((agent) => {
        // 边缘场景：skills 为空且无删除 → 不显示该段
        const skillsDiff = diffs?.get(agent.name)?.skills ?? null;
        const removedCount = skillsDiff?.removed.length ?? 0;
        if (agent.skills.length === 0 && removedCount === 0) return null;

        return (
          <div key={agent.name} className="element-agent-group">
            <div className="element-agent-group-head">
              <AgentBadge name={agent.name} />
              <h4>
                Skills（{agent.skills.length}
                {showDiff && removedCount > 0 && ` +${removedCount} 已删除`}）
              </h4>
            </div>

            {/* 当前版本 skills：added 标绿，其余正常 */}
            {agent.skills.map((sk, i) => {
              const isAdded = showDiff && skillsDiff?.added.includes(sk.path);
              return (
                <div key={i} className={`skill-row ${isAdded ? "diff-add" : ""}`}>
                  <span>{sk.path}</span>
                  {sk.load_error && <span className="skill-load-error">⚠ {sk.load_error}</span>}
                </div>
              );
            })}

            {/* 删除的 skills：当前版本没有，从 diff 补出来标红 */}
            {showDiff &&
              skillsDiff?.removed.map((path, i) => (
                <div key={`del-${i}`} className="skill-row diff-del">
                  <span>{path}</span>
                </div>
              ))}
          </div>
        );
      })}
    </div>
  );
}
