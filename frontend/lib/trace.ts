import type {
  TraceContextSegment,
  TraceDetail,
  TraceLogEvent,
  TraceNode,
  TraceRunSummary,
  TraceTodoSnapshot,
} from "./types";

export function appendLiveTraceEvent(
  current: TraceDetail | null,
  event: TraceLogEvent,
  fallbackRun: TraceRunSummary,
): TraceDetail {
  const baseRun = current?.run.trace_id === event.trace_id ? current.run : fallbackRun;
  const run = updateRunFromEvent(baseRun, event);
  const events = [...(current?.run.trace_id === event.trace_id ? current.events : []).filter((item) => item.event_id !== event.event_id), event].sort(
    (left, right) => left.sequence - right.sequence,
  );

  return projectTraceDetail(run, events);
}

function projectTraceDetail(run: TraceRunSummary, events: TraceLogEvent[]): TraceDetail {
  const nodes: TraceNode[] = [runNode(run, events)];
  const context: TraceContextSegment[] = [];
  const todos: TraceTodoSnapshot[] = [];
  const agentNodeIds = new Set<string>();
  const pendingLlm = new Map<string, TraceLogEvent[]>();
  const pendingTool = new Map<string, TraceLogEvent[]>();
  // task 边界追踪
  let currentTaskCallId: string | null = null;
  const agentLastTask = new Map<string, string | null>();
  const agentInvocationCounter = new Map<string, number>();
  const instanceFirstTs = new Map<string, string>();
  const instanceLastTs = new Map<string, string>();

  function getAgentNodeId(event: TraceLogEvent): string {
    if (!event.agent_name) return "run";
    if (agentRole(event.agent_name) === "main") return `agent:${event.agent_name}`;
    const invocation = agentInvocationCounter.get(event.agent_name);
    if (invocation != null) return `agent:${event.agent_name}:${invocation}`;
    return `agent:${event.agent_name}`; // D9 回退
  }

  function ensureAgentNode(event: TraceLogEvent) {
    if (!event.agent_name) return;

    // ── Meta-agent：保持全局唯一 ──
    if (agentRole(event.agent_name) === "main") {
      const nodeId = `agent:${event.agent_name}`;
      if (agentNodeIds.has(nodeId)) return;
      agentNodeIds.add(nodeId);
      nodes.push({
        node_id: nodeId,
        parent_node_id: "run",
        kind: "agent",
        label: event.agent_name,
        status: "completed",
        agent_name: event.agent_name,
        agent_role: agentRole(event.agent_name),
        depth: agentDepth(event.agent_name),
        started_at: event.timestamp,
        raw_event_ids: [event.event_id],
        chain_summary: agentSummary(event),
      });
      return;
    }

    // ── Subagent：实例化拆分 ──
    const currentTask = currentTaskCallId;
    const lastTask = agentLastTask.get(event.agent_name);

    if (lastTask != null && lastTask === currentTask) return; // 同一实例内

    // 新实例
    const counter = (agentInvocationCounter.get(event.agent_name) ?? 0) + 1;
    agentInvocationCounter.set(event.agent_name, counter);
    agentLastTask.set(event.agent_name, currentTask);

    const nodeId = `agent:${event.agent_name}:${counter}`;
    agentNodeIds.add(nodeId);
    instanceFirstTs.set(nodeId, event.timestamp);

    // 确定父节点
    let parentId = "run";
    if (isEvaluationAgent(event.agent_name)) {
      const primaryName = evaluationPrimaryAgentName(event.agent_name);
      const primaryInv = agentInvocationCounter.get(primaryName) ?? 1;
      parentId = `agent:${primaryName}:${primaryInv}`;
    }

    nodes.push({
      node_id: nodeId,
      parent_node_id: parentId,
      kind: "agent",
      label: event.agent_name,
      status: "completed",
      agent_name: event.agent_name,
      agent_role: agentRole(event.agent_name),
      depth: agentDepth(event.agent_name),
      started_at: event.timestamp,
      raw_event_ids: [event.event_id],
      chain_summary: agentSummary(event),
    });
  }

  function appendContext(event: TraceLogEvent, nodeId: string, kind: TraceContextSegment["kind"], title: string, content: unknown) {
    const segment: TraceContextSegment = {
      anchor_id: `ctx-${event.sequence}-${context.length + 1}`,
      sequence: context.length + 1,
      kind,
      agent_name: event.agent_name,
      agent_role: agentRole(event.agent_name),
      depth: agentDepth(event.agent_name),
      title,
      content: content ?? "",
      metadata: {
        phase: event.type.endsWith("_error") ? "error" : "output",
        event_type: event.type,
        status: event.status,
        duration_ms: event.duration_ms,
        model_name: event.model_name,
        tool_name: event.tool_name,
      },
      tool_call_names: toolCallNames(event.tool_calls),
      related_node_id: nodeId,
      collapsed_by_default: false,
    };
    context.push(segment);
    return segment.anchor_id;
  }

  for (const event of events) {
    // ── N2: task 工具拦截 ──
    if (event.tool_name === "task" && ["tool_start", "tool_end", "tool_error"].includes(event.type)) {
      if (event.type === "tool_start") {
        currentTaskCallId = event.tool_call_id ?? null;
      } else {
        currentTaskCallId = null;
      }
      continue;
    }

    if (["llm_start", "llm_end", "llm_error", "tool_start", "tool_end", "tool_error"].includes(event.type)) {
      ensureAgentNode(event);
    }

    // ── 实例时间戳追踪 ──
    if (event.agent_name && agentRole(event.agent_name) === "subagent") {
      const instanceId = getAgentNodeId(event);
      instanceLastTs.set(instanceId, event.timestamp);
    }

    if (event.type === "llm_start") {
      const key = pairKey(event);
      const list = pendingLlm.get(key) ?? [];
      list.push(event);
      pendingLlm.set(key, list);
      continue;
    }

    if (event.type === "tool_start") {
      const key = pairKey(event);
      const list = pendingTool.get(key) ?? [];
      list.push(event);
      pendingTool.set(key, list);
      continue;
    }

    if (event.type === "llm_end" || event.type === "llm_error") {
      const key = pairKey(event);
      const list = pendingLlm.get(key);
      const start = list?.shift();
      if (list && list.length === 0) pendingLlm.delete(key);
      const nodeId = llmNodeId(start ?? event);
      const anchorId = appendContext(
        event,
        nodeId,
        event.type === "llm_error" ? "error" : "ai",
        event.type === "llm_error" ? "LLM 失败" : "AI 输出",
        event.type === "llm_error" ? event.error : llmContent(event),
      );
      nodes.push({
        node_id: nodeId,
        parent_node_id: getAgentNodeId(event),
        kind: event.type === "llm_error" ? "error" : "llm",
        label: event.model_name || "LLM",
        status: event.status,
        agent_name: event.agent_name,
        agent_role: agentRole(event.agent_name),
        depth: agentDepth(event.agent_name),
        started_at: start?.timestamp,
        ended_at: event.timestamp,
        duration_ms: event.duration_ms,
        model_name: event.model_name,
        usage: event.usage,
        context_anchor_id: anchorId,
        output_context_anchor_id: anchorId,
        raw_event_ids: start ? [start.event_id, event.event_id] : [event.event_id],
        error: event.error,
        chain_summary: event.type === "llm_error" ? errorSummary(event.error) : llmSummary(event),
      });
      continue;
    }

    if (event.type === "tool_end" || event.type === "tool_error") {
      const key = pairKey(event);
      const list = pendingTool.get(key);
      const start = list?.shift();
      if (list && list.length === 0) pendingTool.delete(key);
      const nodeId = toolNodeId(start ?? event);
      const anchorId = appendContext(
        event,
        nodeId,
        event.type === "tool_error" ? "error" : "tool",
        event.type === "tool_error" ? "Tool 失败" : `Tool 输出 · ${event.tool_name || "Tool"}`,
        event.type === "tool_error" ? event.error : event.tool_output,
      );
      nodes.push({
        node_id: nodeId,
        parent_node_id: getAgentNodeId(event),
        kind: event.type === "tool_error" ? "error" : "tool",
        label: event.tool_name || "Tool",
        status: event.status,
        agent_name: event.agent_name,
        agent_role: agentRole(event.agent_name),
        depth: agentDepth(event.agent_name),
        started_at: start?.timestamp,
        ended_at: event.timestamp,
        duration_ms: event.duration_ms,
        tool_name: event.tool_name,
        context_anchor_id: anchorId,
        output_context_anchor_id: anchorId,
        raw_event_ids: start ? [start.event_id, event.event_id] : [event.event_id],
        error: event.error,
        chain_summary: event.type === "tool_error" ? errorSummary(event.error, event.tool_output) : toolSummary(event.tool_name, event.tool_output),
      });
      continue;
    }

    if (event.type === "run_error") {
      const nodeId = `error:${event.event_id}`;
      const anchorId = appendContext(event, nodeId, "error", "Run 失败", event.error);
      nodes.push({
        node_id: nodeId,
        parent_node_id: "run",
        kind: "error",
        label: "Run 失败",
        status: "failed",
        depth: 0,
        context_anchor_id: anchorId,
        output_context_anchor_id: anchorId,
        raw_event_ids: [event.event_id],
        error: event.error,
        chain_summary: errorSummary(event.error),
      });
    }
  }

  // ── 回填 agent 节点的 duration ──
  for (const node of nodes) {
    if (node.kind !== "agent") continue;
    const first = instanceFirstTs.get(node.node_id);
    const last = instanceLastTs.get(node.node_id);
    if (first && last) {
      node.ended_at = last;
      node.duration_ms = tsDiffMs(first, last);
    }
  }

  for (const events of pendingLlm.values()) {
    for (const event of events) {
      nodes.push({
        node_id: llmNodeId(event),
        parent_node_id: getAgentNodeId(event),
        kind: "llm",
        label: event.model_name || "LLM",
        status: "running",
        agent_name: event.agent_name,
        agent_role: agentRole(event.agent_name),
        depth: agentDepth(event.agent_name),
        started_at: event.timestamp,
        model_name: event.model_name,
        raw_event_ids: [event.event_id],
        chain_summary: llmSummary(event, true),
      });
    }
  }

  for (const events of pendingTool.values()) {
    for (const event of events) {
      nodes.push({
        node_id: toolNodeId(event),
        parent_node_id: getAgentNodeId(event),
        kind: "tool",
        label: event.tool_name || "Tool",
        status: "running",
        agent_name: event.agent_name,
        agent_role: agentRole(event.agent_name),
        depth: agentDepth(event.agent_name),
        started_at: event.timestamp,
        tool_name: event.tool_name,
        raw_event_ids: [event.event_id],
        chain_summary: `${event.tool_name || "Tool"}: 运行中…`,
      });
    }
  }

  return { run, events, nodes, context, todos };
}

// ── chain_summary 生成函数（前端版本，与后端 chain_summary.py 保持一致） ──

function runSummary(run: TraceRunSummary): string {
  const statusLabel = run.status === "completed" ? "完成" : run.status === "failed" ? "失败" : "运行中";
  const duration = run.duration_ms != null ? `${(run.duration_ms / 1000).toFixed(run.duration_ms < 10000 ? 1 : 0)}s` : "--";
  return `${run.endpoint} · ${statusLabel} · ${duration}`;
}

function agentSummary(event: TraceLogEvent): string {
  const name = event.agent_name || "Unknown";
  const role = (event.agent_name || "").endsWith("-subagent") ? "子代理" : "主代理";
  return `${name} · ${role}`;
}

function llmSummary(event: TraceLogEvent, isRunning = false): string {
  const model = event.model_name || "LLM";
  if (isRunning) return `${model}: 运行中…`;
  const text = extractLlmText(event.output);
  if (text) {
    const truncated = text.length > 100 ? text.slice(0, 100) + "…" : text;
    return `${model}: ${truncated}`;
  }
  // content 为空但有 tool_calls → 直接列工具名
  const calls = toolCallNames(event.tool_calls);
  if (calls.length > 0) {
    return `${model}: ${calls.join(", ")}`;
  }
  return `${model}: (无输出)`;
}

function toolSummary(toolName: string | null | undefined, toolOutput: unknown): string {
  if (!toolName) return `Tool: ${truncate(String(toolOutput), 80)}`;
  const builders: Record<string, (out: unknown) => string> = {
    write_file: (out) => `write_file: ${extractPath(out)}`,
    write: (out) => `write_file: ${extractPath(out)}`,
    read_file: (out) => `read_file: ${extractPath(out)}`,
    read: (out) => `read_file: ${extractPath(out)}`,
    set_goal: (out) => `set_goal: ${truncate(extractGoal(out), 50)}`,
    record_goal_completion: (out) => `goal_completion: ${truncate(extractCompleted(out), 50)}`,
    update_todo_list: () => "update_todo_list",
  };
  const builder = builders[toolName];
  return builder ? builder(toolOutput) : `${toolName}`;
}

function errorSummary(error: string | null | undefined, toolOutput?: unknown): string {
  if (error) return `❌ ${truncate(error, 200)}`;
  if (toolOutput != null) return `❌ ${truncate(String(toolOutput), 200)}`;
  return "❌ 未知错误";
}

function extractLlmText(output: unknown): string {
  if (output == null) return "";
  if (typeof output === "string") return output;
  if (typeof output === "object" && output !== null && "messages" in output) {
    const messages = (output as { messages?: unknown[] }).messages;
    if (Array.isArray(messages)) {
      for (let i = messages.length - 1; i >= 0; i--) {
        const msg = messages[i];
        if (!msg || typeof msg !== "object") continue;
        const role = ((msg as Record<string, unknown>).type ?? (msg as Record<string, unknown>).role ?? "") as string;
        if (role === "ai" || role === "assistant") {
          const content = (msg as Record<string, unknown>).content;
          if (typeof content === "string") return content;
          if (Array.isArray(content)) {
            return content
              .map((block) => (block && typeof block === "object" && "text" in block ? String(block.text) : ""))
              .filter(Boolean)
              .join("\n");
          }
        }
      }
    }
  }
  return String(output);
}

function extractPath(output: unknown): string {
  if (typeof output === "string") return truncate(output, 60);
  if (typeof output === "object" && output !== null) {
    for (const key of ["path", "file_path", "filename"]) {
      const value = (output as Record<string, unknown>)[key];
      if (typeof value === "string") return value;
    }
    const content = (output as Record<string, unknown>).content;
    if (typeof content === "string") return truncate(content, 60);
  }
  return truncate(String(output), 60);
}

function extractGoal(output: unknown): string {
  if (typeof output === "object" && output !== null) {
    for (const key of ["goal", "content", "text"]) {
      const value = (output as Record<string, unknown>)[key];
      if (typeof value === "string") return value;
    }
  }
  return String(output);
}

function extractCompleted(output: unknown): string {
  if (typeof output === "object" && output !== null) {
    for (const key of ["goal", "content", "completed"]) {
      const value = (output as Record<string, unknown>)[key];
      if (typeof value === "string") return truncate(value, 50);
    }
  }
  return truncate(String(output), 50);
}

function truncate(str: string, max: number): string {
  return str.length > max ? str.slice(0, max) : str;
}

// ── 辅助函数 ──

function updateRunFromEvent(run: TraceRunSummary, event: TraceLogEvent): TraceRunSummary {
  if (event.type === "run_end" || event.type === "run_error") {
    return {
      ...run,
      status: event.status,
      ended_at: event.timestamp,
      duration_ms: event.duration_ms ?? run.duration_ms,
      event_count: event.sequence,
      error: event.error ?? run.error,
    };
  }
  return { ...run, status: event.status === "failed" ? "failed" : run.status, event_count: Math.max(run.event_count, event.sequence) };
}

function runNode(run: TraceRunSummary, events: TraceLogEvent[]): TraceNode {
  return {
    node_id: "run",
    kind: "run",
    label: run.endpoint,
    status: run.status,
    started_at: run.started_at,
    ended_at: run.ended_at,
    duration_ms: run.duration_ms,
    depth: 0,
    raw_event_ids: events.map((event) => event.event_id),
    error: run.error,
    chain_summary: runSummary(run),
  };
}

function pairKey(event: TraceLogEvent) {
  if (event.tool_call_id) return `tool_call:${event.tool_call_id}`;
  if (event.run_id) return `run:${event.run_id}`;
  if (event.type?.startsWith("llm")) return `llm:${event.agent_name || "unknown"}:${event.model_name || "unknown"}`;
  if (event.type?.startsWith("tool")) return `tool:${event.agent_name || "unknown"}:${event.tool_name || "unknown"}`;
  return `event:${event.agent_name || "unknown"}`;
}

function agentNodeId(agentName?: string | null) {
  return agentName ? `agent:${agentName}` : "run";
}

function tsDiffMs(start: string, end: string): number | null {
  try {
    const t0 = new Date(start).getTime();
    const t1 = new Date(end).getTime();
    if (isNaN(t0) || isNaN(t1)) return null;
    return t1 - t0;
  } catch {
    return null;
  }
}

function llmNodeId(event: TraceLogEvent) {
  return `llm:${event.event_id}`;
}

function toolNodeId(event: TraceLogEvent) {
  return `tool:${event.event_id}`;
}

function agentRole(agentName?: string | null) {
  if (!agentName) return null;
  return agentName.endsWith("-subagent") ? "subagent" : "main";
}

function isEvaluationAgent(agentName?: string | null): boolean {
  return !!agentName && agentName.includes("evaluation");
}

function evaluationPrimaryAgentName(agentName: string): string {
  if (agentName === "evaluation-subagent") return "outline-subagent";
  return agentName.replace("-evaluation", "");
}

function agentDepth(agentName?: string | null) {
  if (!agentName) return 0;
  if (isEvaluationAgent(agentName)) return 2;
  return agentName.endsWith("-subagent") ? 1 : 0;
}

function toolCallNames(toolCalls: unknown) {
  if (!Array.isArray(toolCalls)) return [];
  return toolCalls.map((call) => (typeof call === "object" && call !== null && "name" in call ? String(call.name) : "")).filter(Boolean);
}

function llmContent(event: TraceLogEvent) {
  const output = event.output;
  if (typeof output === "string") return output;
  if (output && typeof output === "object" && "messages" in output) {
    const messages = (output as { messages?: unknown }).messages;
    const lastMessage = Array.isArray(messages) ? messages.findLast((message) => messageKind(message) === "ai") : null;
    const content = messageContent(lastMessage);
    if (content) return content;
  }
  return output ?? "";
}

function messageKind(message: unknown) {
  if (!message || typeof message !== "object") return "";
  const value = message as Record<string, unknown>;
  return String(value.type ?? value.role ?? value._getType ?? "").toLowerCase();
}

function messageContent(message: unknown) {
  if (!message || typeof message !== "object") return "";
  const content = (message as Record<string, unknown>).content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((item) => (item && typeof item === "object" && "text" in item ? String(item.text) : ""))
    .filter(Boolean)
    .join("\n");
}
