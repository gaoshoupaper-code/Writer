import { useState } from "react";
import type { VersionChanges, AgentDiff, IntentItem } from "@/lib/api";
import { AgentBadge } from "./AgentBadge";

/**
 * 升级总览条（页面门面）。
 *
 * 选一个版本后，第一眼看到"这个版本相比父版本改了什么"：
 * - 客观摘要：遍历 changes.agents，每 agent 一行（prompt ±N 行 / skills ±N / middleware 增删改）
 * - 改动意图：design_doc 的 intent 列表，默认收起，可展开看 reason / expected_up / expected_down
 *
 * 首版本（无 parent）→ 显示"初始版本"占位，不渲染 diff。
 */
export function UpgradeOverview({
  changes,
  isBootstrap,
}: {
  changes: VersionChanges | null;
  isBootstrap: boolean;
}) {
  // D7：首版本显示占位
  if (isBootstrap) {
    return (
      <div className="upgrade-overview">
        <h3 className="upgrade-overview-title">📌 本版本升级</h3>
        <p className="upgrade-bootstrap">这是初始版本，无升级对比。</p>
      </div>
    );
  }

  // 无 diff 数据（version_changes 表为空，或 agents 全空 + intent 为 null）
  const hasAgents = changes && changes.agents.length > 0;
  const hasIntent = changes && changes.intent && changes.intent.length > 0;
  if (!hasAgents && !hasIntent) {
    return (
      <div className="upgrade-overview">
        <h3 className="upgrade-overview-title">📌 本版本升级</h3>
        <p className="upgrade-bootstrap">本版本无要素变更记录。</p>
      </div>
    );
  }

  return (
    <div className="upgrade-overview">
      <h3 className="upgrade-overview-title">📌 本版本升级</h3>

      {/* 客观 diff 摘要 */}
      {hasAgents && (
        <div className="upgrade-summary-list">
          {changes!.agents.map(({ agent, diff }) => (
            <div key={agent} className="upgrade-summary-item">
              <AgentBadge name={agent} />
              <span className="upgrade-change-desc">{summarizeAgentDiff(diff)}</span>
            </div>
          ))}
        </div>
      )}

      {/* 改动意图（可折叠） */}
      {hasIntent && <IntentSection intent={changes!.intent!} />}
    </div>
  );
}

/**
 * 把单个 agent 的三要素 diff 压成一句人话摘要。
 * whole_agent 存在时只说整 agent 增删；否则分别说 prompt/skills/middleware。
 */
function summarizeAgentDiff(diff: AgentDiff): string {
  if (diff.whole_agent === "added") return "新增 Agent";
  if (diff.whole_agent === "removed") return "删除 Agent";

  const parts: string[] = [];

  if (diff.prompt) {
    const { added, removed } = diff.prompt.summary;
    parts.push(`prompt +${added}/-${removed} 行`);
  }

  if (diff.skills) {
    const a = diff.skills.added.length;
    const r = diff.skills.removed.length;
    if (a || r) parts.push(`skills +${a}/-${r}`);
  }

  if (diff.processors && diff.processors.length > 0) {
    const added = diff.processors.filter((p) => p.change_type === "added").length;
    const removed = diff.processors.filter((p) => p.change_type === "removed").length;
    const modified = diff.processors.filter((p) => p.change_type === "modified").length;
    const segs: string[] = [];
    if (added) segs.push(`${added} 新增`);
    if (removed) segs.push(`${removed} 删除`);
    if (modified) segs.push(`${modified} 修改`);
    if (segs.length) parts.push(`middleware ${segs.join(" / ")}`);
  }

  return parts.length ? parts.join("，") : "无变化";
}

/** 改动意图折叠区块：design_doc 的每条改动（target + desc + reason + expected） */
function IntentSection({ intent }: { intent: IntentItem[] }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="intent-section">
      <div className="intent-toggle" onClick={() => setOpen(!open)}>
        <span>{open ? "▾" : "▸"}</span>
        改动意图（{intent.length} 条）
      </div>
      {open && (
        <div className="intent-list">
          {intent.map((item, i) => (
            <div key={i} className="intent-item">
              <div className="intent-target">{item.target || "（未指定目标）"}</div>
              <div className="intent-desc">{item.change_desc || "（无描述）"}</div>
              {(item.reason || item.expected_up || item.expected_down) && (
                <div className="intent-meta">
                  {item.reason && (
                    <span>
                      <label>依据：</label>
                      {item.reason}
                    </span>
                  )}
                  {item.expected_up && (
                    <span className="intent-up">
                      <label>预期↑：</label>
                      {item.expected_up}
                    </span>
                  )}
                  {item.expected_down && (
                    <span className="intent-down">
                      <label>预期↓：</label>
                      {item.expected_down}
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
