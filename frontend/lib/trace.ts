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

  function ensureAgentNode(event: TraceLogEvent) {
    if (!event.agent_name) return;
    const nodeId = agentNodeId(event.agent_name);
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
    if (["llm_start", "llm_end", "llm_error", "tool_start", "tool_end", "tool_error"].includes(event.type)) {
      ensureAgentNode(event);
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
        parent_node_id: agentNodeId(event.agent_name),
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
        parent_node_id: agentNodeId(event.agent_name),
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
      });
    }
  }

  for (const events of pendingLlm.values()) {
    for (const event of events) {
      nodes.push({
        node_id: llmNodeId(event),
        parent_node_id: agentNodeId(event.agent_name),
        kind: "llm",
        label: event.model_name || "LLM",
        status: "running",
        agent_name: event.agent_name,
        agent_role: agentRole(event.agent_name),
        depth: agentDepth(event.agent_name),
        started_at: event.timestamp,
        model_name: event.model_name,
        raw_event_ids: [event.event_id],
      });
    }
  }

  for (const events of pendingTool.values()) {
    for (const event of events) {
      nodes.push({
        node_id: toolNodeId(event),
        parent_node_id: agentNodeId(event.agent_name),
        kind: "tool",
        label: event.tool_name || "Tool",
        status: "running",
        agent_name: event.agent_name,
        agent_role: agentRole(event.agent_name),
        depth: agentDepth(event.agent_name),
        started_at: event.timestamp,
        tool_name: event.tool_name,
        raw_event_ids: [event.event_id],
      });
    }
  }

  return { run, events, nodes, context, todos };
}

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

function llmNodeId(event: TraceLogEvent) {
  return `llm:${event.event_id}`;
}

function toolNodeId(event: TraceLogEvent) {
  return `tool:${event.event_id}`;
}

function agentRole(agentName?: string | null) {
  if (!agentName) return null;
  return agentName === "meta-agent" ? "main" : "subagent";
}

function agentDepth(agentName?: string | null) {
  if (!agentName) return 0;
  return agentName === "meta-agent" ? 1 : 2;
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
