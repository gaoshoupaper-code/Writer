"use client";

/**
 * 版本差异展示面板（/versions 详情面板「差异」Tab）。
 *
 * 渲染 version_changes 数据（GET /api/versions/{n}.changes）：
 *   - 顶部「本轮进化意图」区块（design_doc 的 reason/expected，D-T1）
 *   - 按 agent 折叠分组，每组内三要素分区（prompt / skills / processors）
 *
 * prompt 用行级红绿高亮（D-T6 hunk 序列）；skills/processors 用增删标记。
 *
 * 设计依据：设计文档 D-T12（API 契约）/ D-T13（两 Tab）/ D-T3（三要素 diff）。
 */
import type {
  AgentDiff,
  IntentItem,
  ProcessorChange,
  PromptDiff,
  SkillsDiff,
  VersionChanges,
} from "@/lib/adapt-types";

// agent 名展示友好映射
const AGENT_LABEL: Record<string, string> = {
  meta_pipeline: "编排者 (meta)",
  storybuilding: "故事构建 (storybuilding)",
  detail_outline: "细节大纲 (detail_outline)",
  writing: "写作 (writing)",
  interview: "访谈 (interview)",
  general_purpose: "通用 (general_purpose)",
};

function agentLabel(agent: string): string {
  return AGENT_LABEL[agent] ?? agent;
}

export function VersionDiffPanel({ changes }: { changes: VersionChanges }) {
  const hasAgents = changes.agents.length > 0;
  const hasIntent = changes.intent && changes.intent.length > 0;

  if (!hasAgents && !hasIntent) {
    return (
      <div className="diff-empty">
        <p className="text-dim">该版本无差异数据。</p>
        <p className="text-mute" style={{ fontSize: 12, marginTop: 8 }}>
          可能是首版本（无 parent），或与 parent 版本 config 完全相同。
        </p>
      </div>
    );
  }

  return (
    <div className="version-diff">
      {hasIntent && <IntentSection intent={changes.intent!} />}
      {hasAgents && (
        <div className="diff-agents">
          <h3 className="section-title">配置差异（相比 v 的上一版）</h3>
          {changes.agents.map((a) => (
            <AgentDiffBlock key={a.agent} agent={a.agent} diff={a.diff} />
          ))}
        </div>
      )}
    </div>
  );
}

// ── 意图区块 ──────────────────────────────────────────────────

function IntentSection({ intent }: { intent: IntentItem[] }) {
  return (
    <div className="diff-intent-section">
      <h3 className="section-title">本轮进化意图（design_doc）</h3>
      <p className="text-mute diff-section-hint">
        来自方案设计文档。target 是自由文本，可能与下方配置差异无法逐条对应——请整体参照。
      </p>
      <div className="intent-list">
        {intent.map((item, i) => (
          <div key={i} className="intent-item">
            <div className="intent-target">{item.target || "（未命名改动）"}</div>
            {item.change_desc && (
              <div className="intent-line">
                <span className="intent-key">改什么</span>
                <span>{item.change_desc}</span>
              </div>
            )}
            {item.reason && (
              <div className="intent-line">
                <span className="intent-key">为什么</span>
                <span className="text-dim">{item.reason}</span>
              </div>
            )}
            {(item.expected_up || item.expected_down) && (
              <div className="intent-line">
                <span className="intent-key">预期</span>
                <span className="mono" style={{ fontSize: 11 }}>
                  {item.expected_up && <span style={{ color: "var(--completed)" }}>↑ {item.expected_up}</span>}
                  {item.expected_up && item.expected_down && <span className="text-mute">　</span>}
                  {item.expected_down && <span style={{ color: "var(--failed)" }}>↓ {item.expected_down}</span>}
                </span>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── 单 agent diff 块 ──────────────────────────────────────────

function AgentDiffBlock({ agent, diff }: { agent: string; diff: AgentDiff }) {
  const wholeBadge =
    diff.whole_agent === "added" ? "新增 agent" : diff.whole_agent === "removed" ? "删除 agent" : null;

  const hasPrompt = diff.prompt != null;
  const hasSkills = diff.skills != null;
  const hasProcessors = diff.processors.length > 0;

  // 变化计数摘要
  const counts: string[] = [];
  if (hasPrompt) counts.push(`prompt +${diff.prompt!.summary.added}/-${diff.prompt!.summary.removed}`);
  if (hasSkills) counts.push(`skills +${diff.skills!.added.length}/-${diff.skills!.removed.length}`);
  if (hasProcessors) {
    const added = diff.processors.filter((p) => p.change_type === "added").length;
    const removed = diff.processors.filter((p) => p.change_type === "removed").length;
    const modified = diff.processors.filter((p) => p.change_type === "modified").length;
    counts.push(`middleware +${added}/-${removed}/~${modified}`);
  }

  return (
    <details className="agent-diff-block" open>
      <summary className="agent-diff-head">
        <span className="agent-diff-name">{agentLabel(agent)}</span>
        {wholeBadge && <span className="agent-whole-badge">{wholeBadge}</span>}
        <span className="agent-diff-counts mono text-mute">{counts.join(" · ")}</span>
      </summary>
      <div className="agent-diff-body">
        {hasPrompt && <PromptDiffView diff={diff.prompt!} />}
        {hasSkills && <SkillsDiffView diff={diff.skills!} />}
        {hasProcessors && (
          <div className="element-section">
            <div className="element-title">Middleware</div>
            {diff.processors.map((p, i) => (
              <ProcessorChangeRow key={i} change={p} />
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

// ── prompt 行级 diff ──────────────────────────────────────────

function PromptDiffView({ diff }: { diff: PromptDiff }) {
  return (
    <div className="element-section">
      <div className="element-title">
        Prompt
        <span className="mono text-mute" style={{ fontSize: 11, marginLeft: 8 }}>
          +{diff.summary.added} 行 / -{diff.summary.removed} 行
        </span>
      </div>
      <div className="prompt-diff">
        {diff.hunks.map((hunk, hi) =>
          hunk.lines.map((line, li) => {
            const key = `${hi}-${li}`;
            if (hunk.type === "equal") {
              return (
                <div key={key} className="diff-line diff-line-ctx">
                  <span className="diff-line-prefix"> </span>
                  <span className="diff-line-text text-mute">{line || " "}</span>
                </div>
              );
            }
            if (hunk.type === "insert") {
              return (
                <div key={key} className="diff-line diff-line-add">
                  <span className="diff-line-prefix">+</span>
                  <span className="diff-line-text">{line || " "}</span>
                </div>
              );
            }
            return (
              <div key={key} className="diff-line diff-line-del">
                <span className="diff-line-prefix">-</span>
                <span className="diff-line-text">{line || " "}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// ── skills diff ───────────────────────────────────────────────

function SkillsDiffView({ diff }: { diff: SkillsDiff }) {
  return (
    <div className="element-section">
      <div className="element-title">Skills</div>
      <div className="skills-diff">
        {diff.added.map((s) => (
          <div key={`a-${s}`} className="skill-row skill-added">
            <span className="diff-line-prefix">+</span>
            <span className="mono">{s}</span>
          </div>
        ))}
        {diff.removed.map((s) => (
          <div key={`r-${s}`} className="skill-row skill-removed">
            <span className="diff-line-prefix">-</span>
            <span className="mono">{s}</span>
          </div>
        ))}
        {diff.unchanged_count > 0 && (
          <div className="skill-unchanged text-mute mono" style={{ fontSize: 11 }}>
            （{diff.unchanged_count} 个 skill 未变）
          </div>
        )}
      </div>
    </div>
  );
}

// ── processor(middleware) 变化行 ──────────────────────────────

function ProcessorChangeRow({ change }: { change: ProcessorChange }) {
  const typeLabel: Record<string, string> = { added: "新增", removed: "删除", modified: "修改" };
  const typeClass: Record<string, string> = {
    added: "proc-added",
    removed: "proc-removed",
    modified: "proc-modified",
  };
  const classChanged = change.class_change.old !== change.class_change.new;
  const paramsChanged = JSON.stringify(change.params_change.old) !== JSON.stringify(change.params_change.new);

  return (
    <div className={`proc-row ${typeClass[change.change_type]}`}>
      <div className="proc-head">
        <span className={`proc-badge ${typeClass[change.change_type]}`}>{typeLabel[change.change_type]}</span>
        <span className="mono proc-key">
          {change.key.hook} / {change.key.group}
        </span>
        <span className="mono text-mute proc-class">
          {change.class_change.new ?? change.class_change.old ?? "?"}
        </span>
      </div>
      {classChanged && (
        <div className="proc-detail">
          <span className="intent-key">类</span>
          <span className="mono">
            <span className="proc-old">{change.class_change.old ?? "（无）"}</span>
            <span className="text-mute" style={{ margin: "0 6px" }}>→</span>
            <span className="proc-new">{change.class_change.new ?? "（无）"}</span>
          </span>
        </div>
      )}
      {paramsChanged && (
        <div className="proc-detail">
          <span className="intent-key">参数</span>
          <span className="mono proc-params">
            <span className="proc-old">{JSON.stringify(change.params_change.old) || "{}"}</span>
            <span className="text-mute" style={{ margin: "0 6px" }}>→</span>
            <span className="proc-new">{JSON.stringify(change.params_change.new) || "{}"}</span>
          </span>
        </div>
      )}
    </div>
  );
}
