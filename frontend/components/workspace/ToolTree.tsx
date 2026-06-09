import type { ToolStatus } from "../../lib/types";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";

const TOOL_STATUS_LABELS: Record<ToolStatus["status"], string> = {
  running: "调用中",
  done: "完成",
  failed: "失败",
};

const TOOL_STATUS_VARIANT: Record<ToolStatus["status"], "running" | "completed" | "failed"> = {
  running: "running",
  done: "completed",
  failed: "failed",
};

function ToolTreeItem({ tool, isChild = false }: { tool: ToolStatus; isChild?: boolean }) {
  const isTask = tool.name === "task";

  return (
    <li
      className={[
        "tool-status",
        tool.status,
        isTask ? "is-task" : "",
        isChild ? "is-child" : "",
      ].filter(Boolean).join(" ")}
    >
      {isChild && tool.subagentName ? (
        <Badge variant="muted" className="text-[9px] py-0 px-1.5">{tool.subagentName}</Badge>
      ) : null}
      <span className="tool-status-name">{tool.name}</span>
      {tool.status === "running" ? (
        <span className="flex items-center gap-1.5">
          <Skeleton className="h-2 w-2 rounded-full" />
          <Badge variant="running" className="text-[10px] py-0 px-1.5">{TOOL_STATUS_LABELS[tool.status]}</Badge>
        </span>
      ) : (
        <Badge variant={TOOL_STATUS_VARIANT[tool.status]} className="text-[10px] py-0 px-1.5">
          {TOOL_STATUS_LABELS[tool.status]}
        </Badge>
      )}
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
