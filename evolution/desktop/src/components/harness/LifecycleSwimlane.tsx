import { useState } from "react";
import type { AgentElementView, AgentDiff, ProcessorChange } from "@/lib/api";
import { HOOK_ORDER, HOOK_LABELS, agentLabel } from "@/lib/harness-constants";
import { MiddlewareNode } from "./MiddlewareNode";

/**
 * 生命周期泳道图（D3/D8/D14）。
 *
 * 布局：CSS Grid，首列 agent 名（140px），后 6 列对应 6 个 hook 阶段。
 * 每个 agent 一条泳道（一行），middleware 按 hook 分配到对应格子，格子内纵向堆叠（D22）。
 * 无 middleware 的 agent 行折叠为一行摘要，点击展开（D14）。
 */
export function LifecycleSwimlane({
  agents,
  diffs,
  version,
  hasSource,
}: {
  agents: AgentElementView[];
  diffs: Map<string, AgentDiff> | null;
  version: number;
  hasSource: boolean;
}) {
  return (
    <div className="swimlane">
      <div className="swimlane-scroll">
        <div className="swimlane-grid">
          {/* 表头 */}
          <div className="swimlane-head-cell">Agent</div>
          {HOOK_ORDER.map((hook) => (
            <div key={hook} className="swimlane-head-cell">
              <span className="hook-name">{HOOK_LABELS[hook]?.name ?? hook}</span>
              <span className="hook-desc">{HOOK_LABELS[hook]?.desc ?? ""}</span>
            </div>
          ))}

          {/* 每个 agent 一条泳道 */}
          {agents.map((agent) => (
            <SwimlaneRow
              key={agent.name}
              agent={agent}
              processorChanges={diffs?.get(agent.name)?.processors}
              version={version}
              hasSource={hasSource}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

/** 单个 agent 的泳道行：有 middleware 展开泳道，无则折叠摘要 */
function SwimlaneRow({
  agent,
  processorChanges,
  version,
  hasSource,
}: {
  agent: AgentElementView;
  processorChanges?: ProcessorChange[];
  version: number;
  hasSource: boolean;
}) {
  const mwCount = agent.middlewares.length;
  const [expanded, setExpanded] = useState(mwCount > 0); // 有 middleware 默认展开

  // 无 middleware → 折叠摘要行
  if (mwCount === 0) {
    return (
      <div className="swimlane-row-collapsed" onClick={() => setExpanded(!expanded)}>
        <span>{expanded ? "▾" : "▸"}</span>
        <span>{agentLabel(agent.name)}</span>
        <span className="collapse-count">无 middleware</span>
      </div>
    );
  }

  // 用户手动折叠了
  if (!expanded) {
    return (
      <div className="swimlane-row-collapsed" onClick={() => setExpanded(true)}>
        <span>▸</span>
        <span>{agentLabel(agent.name)}</span>
        <span className="collapse-count">{mwCount} 个 middleware</span>
      </div>
    );
  }

  // 构建 diff 查找表：key="{hook}:{group}" → ProcessorChange
  const changeMap = new Map<string, ProcessorChange>();
  processorChanges?.forEach((pc) => {
    changeMap.set(`${pc.key.hook}:${pc.key.group}`, pc);
  });

  // 按 hook 分组 middleware
  const byHook = new Map<string, AgentElementView["middlewares"]>();
  agent.middlewares.forEach((mw) => {
    const hook = mw.hook ?? "__unhooked__";
    if (!byHook.has(hook)) byHook.set(hook, []);
    byHook.get(hook)!.push(mw);
  });

  return (
    <>
      {/* 行标签（可点击折叠） */}
      <div className="swimlane-row-label" onClick={() => setExpanded(false)}>
        <span style={{ cursor: "pointer" }}>▾ {agentLabel(agent.name)}</span>
      </div>

      {/* 6 个 hook 格子 */}
      {HOOK_ORDER.map((hook) => {
        const mws = byHook.get(hook) ?? [];
        return (
          <div key={hook} className="swimlane-cell">
            {mws.map((mw, i) => {
              const change = changeMap.get(`${mw.hook}:${mw.group}`);
              return (
                <MiddlewareNode
                  key={`${mw.class_name}-${i}`}
                  mw={mw}
                  change={change}
                  version={version}
                  hasSource={hasSource}
                />
              );
            })}
          </div>
        );
      })}

      {/* 未挂载到任何已知 hook 的 middleware（hook=null）单独提示 */}
      {byHook.has("__unhooked__") && (
        <div className="swimlane-row-collapsed" style={{ gridColumn: "1 / -1" }}>
          <span>⚠ {byHook.get("__unhooked__")!.length} 个 middleware 未挂载生命周期</span>
        </div>
      )}
    </>
  );
}
