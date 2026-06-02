import type { ToolStatus } from "../../lib/types";

const TOOL_STATUS_LABELS: Record<ToolStatus["status"], string> = {
  running: "调用中",
  done: "完成",
  failed: "失败",
};

function ToolTreeItem({ tool, isChild = false }: { tool: ToolStatus; isChild?: boolean }) {
  const isTask = tool.name === "task";
  const classNames = ["tool-status", tool.status, isTask ? "is-task" : "", isChild ? "is-child" : ""]
    .filter(Boolean)
    .join(" ");

  return (
    <li className={classNames}>
      {isChild && tool.subagentName ? <span className="subagent-label">{tool.subagentName}</span> : null}
      <span className="tool-status-name">{tool.name}</span>
      <span className="tool-status-state">{TOOL_STATUS_LABELS[tool.status]}</span>
    </li>
  );
}

export function ToolTree({ tools }: { tools: ToolStatus[] }) {
  const rootTools = tools.filter((tool) => !tool.parentKey);
  const knownToolKeys = new Set(tools.map((tool) => tool.key));
  const orderedTools: Array<{ tool: ToolStatus; isChild: boolean }> = [];
  const childToolsByParent = new Map<string, ToolStatus[]>();

  for (const tool of tools) {
    if (tool.parentKey) {
      const bucket = childToolsByParent.get(tool.parentKey) ?? [];
      bucket.push(tool);
      childToolsByParent.set(tool.parentKey, bucket);
    }
  }

  for (const tool of rootTools) {
    orderedTools.push({ tool, isChild: false });
    for (const child of childToolsByParent.get(tool.key) ?? []) {
      orderedTools.push({ tool: child, isChild: true });
    }
  }

  for (const tool of tools) {
    if (tool.parentKey && !knownToolKeys.has(tool.parentKey)) {
      orderedTools.push({ tool, isChild: true });
    }
  }

  return (
    <ul className="tool-status-list" aria-label="工具调用状态">
      {orderedTools.map(({ tool, isChild }) => (
        <ToolTreeItem key={tool.key} tool={tool} isChild={isChild} />
      ))}
    </ul>
  );
}
