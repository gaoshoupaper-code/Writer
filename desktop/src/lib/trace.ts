import type {
  TraceContextSegment,
  TraceDetail,
  TraceLogEvent,
  TraceNode,
  TraceRunSummary,
  TraceTodoItem,
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

interface PendingEvent {
  event: TraceLogEvent;
  nodeId: string;
  parallelGroupId: string | null;
}

const TODO_TOOL_NAMES = new Set(["write_todos", "write_todo"]);

function projectTraceDetail(run: TraceRunSummary, events: TraceLogEvent[]): TraceDetail {
  const nodes: TraceNode[] = [runNode(run, events)];
  const context: TraceContextSegment[] = [];
  const todos: TraceTodoSnapshot[] = [];
  const agentNodeIds = new Set<string>();
  const pendingLlm = new Map<string, PendingEvent[]>();
  const pendingTool = new Map<string, PendingEvent[]>();
  // task running 骨架：tool_start 入队、tool_end/tool_error 出队；循环结束残留项回填 running 节点
  const pendingTask = new Map<string, { event: TraceLogEvent; nodeId: string; startedAt: string }>();

  // ── 并行检测局部状态 ──
  const parallelTcToGroup = new Map<string, string>();  // tool_call_id → group_id
  const parallelRunToGroup = new Map<string, string>();  // run_id → group_id
  let pgCounter = 0;

  // task 边界追踪（栈结构：支持嵌套 task 委托）
  const taskCallStack: string[] = [];
  // D4: task 工具 started_at 暂存，tool_end/tool_error 时建轻量 task 节点用
  const taskStartTs = new Map<string, string>();
  const agentLastTask = new Map<string, string | null>();
  const agentInvocationCounter = new Map<string, number>();
  const instanceFirstTs = new Map<string, string>();
  const instanceLastTs = new Map<string, string>();

  function getAgentNodeId(event: TraceLogEvent): string {
    if (!event.agent_name) return "run";
    const name = event.agent_name;
    if (agentRole(name) === "main") return `agent:${name}`;
    const invocation = agentInvocationCounter.get(name);
    if (invocation != null) return `agent:${name}:${invocation}`;
    return `agent:${name}`; // D9 回退
  }

  function nodeAgentAttrs(event: TraceLogEvent) {
    const name = event.agent_name;
    return { agent_name: name, agent_role: agentRole(name), depth: agentDepth(name) };
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

    // ── Subagent（含 evaluation agent）：实例化拆分 ──
    const currentTask = taskCallStack.length > 0 ? taskCallStack[taskCallStack.length - 1] : null;
    const lastTask = agentLastTask.get(event.agent_name);

    if (lastTask != null && lastTask === currentTask) return; // 同一实例内

    // 新实例
    const counter = (agentInvocationCounter.get(event.agent_name) ?? 0) + 1;
    agentInvocationCounter.set(event.agent_name, counter);
    agentLastTask.set(event.agent_name, currentTask);

    const nodeId = `agent:${event.agent_name}:${counter}`;
    agentNodeIds.add(nodeId);
    instanceFirstTs.set(nodeId, event.timestamp);

    // 确定父节点：evaluation agent → 挂到对应 primary subagent 下
    let parentId = "run";
    if (isEvaluationAgent(event.agent_name)) {
      const primaryName = evaluationPrimaryAgentName(event.agent_name);
      const primaryInvocation = agentInvocationCounter.get(primaryName);
      parentId = primaryInvocation != null ? `agent:${primaryName}:${primaryInvocation}` : `agent:${primaryName}`;
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
    const attrs = nodeAgentAttrs(event);
    const segment: TraceContextSegment = {
      anchor_id: `ctx-${event.sequence}-${context.length + 1}`,
      sequence: context.length + 1,
      kind,
      agent_name: attrs.agent_name,
      agent_role: attrs.agent_role,
      depth: attrs.depth,
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
    // ── N2: task 工具拦截 + D4 建轻量 task 节点 ──
    if (event.tool_name === "task" && ["tool_start", "tool_end", "tool_error"].includes(event.type)) {
      if (event.type === "tool_start") {
        if (event.tool_call_id) {
          taskCallStack.push(event.tool_call_id);
          // 清理并行组追踪（防止泄漏）
          parallelTcToGroup.delete(event.tool_call_id);
          taskStartTs.set(event.tool_call_id, event.timestamp);
          // 入 pendingTask：若无对应 tool_end（仍在 running），循环结束回填 running 骨架节点
          pendingTask.set(event.tool_call_id, {
            event,
            nodeId: `task:${event.tool_call_id}`,
            startedAt: event.timestamp,
          });
        }
      } else {
        // tool_end / tool_error：弹出栈 + 建轻量 task 节点（D4）
        // task 节点是 stageFlow 的 subStep 骨架：node_id 含 tool_call_id，供 stageFlow 匹配 message.tools
        if (taskCallStack.length > 0) taskCallStack.pop();
        const callId = event.tool_call_id;
        const nodeId = callId ? `task:${callId}` : `task:${event.event_id}`;
        const isFailed = event.type === "tool_error";
        const rawSummary = skillTextContent(event.tool_output) ?? (event.error ?? "");
        nodes.push({
          node_id: nodeId,
          parent_node_id: "run",
          kind: "task",
          label: "子代理任务",
          status: isFailed || event.status === "failed" ? "failed" : "completed",
          agent_name: event.agent_name,
          agent_role: agentRole(event.agent_name),
          depth: 0,
          started_at: (callId ? taskStartTs.get(callId) : undefined) ?? event.timestamp,
          ended_at: event.timestamp,
          duration_ms: event.duration_ms,
          tool_name: "task",
          raw_event_ids: [event.event_id],
          chain_summary: truncate(rawSummary.trim(), 200) || "子代理任务",
        });
        if (callId) {
          taskStartTs.delete(callId);
          pendingTask.delete(callId);
        }
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
      // 检查 parent_run_id 是否属于并行组（一级传播）
      const pgId = event.parent_run_id ? parallelRunToGroup.get(event.parent_run_id) ?? null : null;
      const nodeId = llmNodeId(event);
      const key = pairKey(event);
      const list = pendingLlm.get(key) ?? [];
      list.push({ event, nodeId, parallelGroupId: pgId });
      pendingLlm.set(key, list);
      continue;
    }

    if (event.type === "tool_start") {
      // 匹配 tool_call_id → 注册 run_id 一级传播
      let pgId: string | null = null;
      if (event.tool_call_id) {
        pgId = parallelTcToGroup.get(event.tool_call_id) ?? null;
        if (pgId && event.run_id) {
          parallelRunToGroup.set(event.run_id, pgId);
        }
      }
      const nodeId = toolNodeId(event);
      const key = pairKey(event);
      const list = pendingTool.get(key) ?? [];
      list.push({ event, nodeId, parallelGroupId: pgId });
      pendingTool.set(key, list);
      continue;
    }

    if (event.type === "llm_end" || event.type === "llm_error") {
      const key = pairKey(event);
      const list = pendingLlm.get(key);
      const pending = list?.shift();
      if (list && list.length === 0) pendingLlm.delete(key);

      // 检测多 tool_calls → 注册并行组
      if (event.type === "llm_end") {
        const calls = event.tool_calls;
        if (Array.isArray(calls) && calls.length > 1) {
          pgCounter++;
          const pgId = `pg-${pgCounter}`;
          for (const tc of calls) {
            if (tc && typeof tc === "object" && "id" in tc) {
              const tcId = String((tc as { id: unknown }).id);
              if (tcId) parallelTcToGroup.set(tcId, pgId);
            }
          }
        }
      }

      const start = pending?.event;
      const nodeId = pending?.nodeId ?? llmNodeId(event);
      const pgId = pending?.parallelGroupId ?? null;
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
        ...nodeAgentAttrs(event),
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
        parallel_group_id: pgId,
      });
      continue;
    }

    if (event.type === "tool_end" || event.type === "tool_error") {
      const key = pairKey(event);
      const list = pendingTool.get(key);
      const pending = list?.shift();
      if (list && list.length === 0) pendingTool.delete(key);

      const start = pending?.event;
      const nodeId = pending?.nodeId ?? toolNodeId(event);
      const pgId = pending?.parallelGroupId ?? null;
      const isToolError = !!toolError(event.tool_output);

      // 检测 SKILL.md 调用
      const skillName = detectSkill(event);
      const nodeKind = skillName ? "skill" : (isToolError ? "error" : "tool");

      const anchorId = appendContext(
        event,
        nodeId,
        isToolError && !skillName ? "error" : (skillName ? "skill" : "tool"),
        isToolError && !skillName ? "Tool 失败" : (skillName ? `Skill 输出 · ${skillName}` : `Tool 输出 · ${event.tool_name || "Tool"}`),
        isToolError && !skillName ? event.error : event.tool_output,
      );

      nodes.push({
        node_id: nodeId,
        parent_node_id: getAgentNodeId(event),
        kind: nodeKind,
        label: skillName ?? (event.tool_name || "Tool"),
        status: isToolError && !skillName ? "failed" : event.status,
        ...nodeAgentAttrs(event),
        started_at: start?.timestamp,
        ended_at: event.timestamp,
        duration_ms: event.duration_ms,
        tool_name: event.tool_name,
        skill_name: skillName,
        context_anchor_id: anchorId,
        output_context_anchor_id: anchorId,
        raw_event_ids: start ? [start.event_id, event.event_id] : [event.event_id],
        error: isToolError && !skillName ? event.error : undefined,
        chain_summary: skillName ? `📖 ${skillName}` : (isToolError ? errorSummary(event.error, event.tool_output) : toolSummary(event.tool_name, event.tool_output)),
        parallel_group_id: pgId,
      });

      // ── Todo 生成 ──
      if (event.tool_name && TODO_TOOL_NAMES.has(event.tool_name)) {
        const items = findTodos(event.tool_output);
        if (items && items.length > 0) {
          const todoNodeId = `todo:${event.event_id}`;
          const todoAnchorId = appendContext(event, todoNodeId, "todo", "Todo 更新", items);
          const activeItem = items.find((item) => item.status === "in_progress")?.content ?? null;
          todos.push({
            anchor_id: todoAnchorId,
            agent_name: event.agent_name,
            items,
            active_item: activeItem,
          });
          nodes.push({
            node_id: todoNodeId,
            parent_node_id: getAgentNodeId(event),
            kind: "todo",
            label: "Todo 更新",
            status: event.status,
            ...nodeAgentAttrs(event),
            started_at: start?.timestamp,
            ended_at: event.timestamp,
            duration_ms: event.duration_ms,
            context_anchor_id: todoAnchorId,
            output_context_anchor_id: todoAnchorId,
            raw_event_ids: start ? [start.event_id, event.event_id] : [event.event_id],
            chain_summary: todoSummary(items),
          });
        }
      }
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

  // ── running 节点（带 parallel_group_id）──
  for (const pendingList of pendingLlm.values()) {
    for (const pending of pendingList) {
      nodes.push({
        node_id: pending.nodeId,
        parent_node_id: getAgentNodeId(pending.event),
        kind: "llm",
        label: pending.event.model_name || "LLM",
        status: "running",
        ...nodeAgentAttrs(pending.event),
        started_at: pending.event.timestamp,
        model_name: pending.event.model_name,
        raw_event_ids: [pending.event.event_id],
        chain_summary: llmSummary(pending.event, true),
        parallel_group_id: pending.parallelGroupId,
      });
    }
  }

  for (const pendingList of pendingTool.values()) {
    for (const pending of pendingList) {
      nodes.push({
        node_id: pending.nodeId,
        parent_node_id: getAgentNodeId(pending.event),
        kind: "tool",
        label: pending.event.tool_name || "Tool",
        status: "running",
        ...nodeAgentAttrs(pending.event),
        started_at: pending.event.timestamp,
        tool_name: pending.event.tool_name,
        raw_event_ids: [pending.event.event_id],
        chain_summary: `${pending.event.tool_name || "Tool"}: 运行中…`,
        parallel_group_id: pending.parallelGroupId,
      });
    }
  }

  // ── running task 骨架节点（pendingTask 回填）──
  // 镜像 tool_end 分支的 completed task 节点，仅 status=running、无 ended_at/duration_ms。
  // 同 node_id 不会与 completed 节点并存：tool_end 已 delete(callId)，残留项必为仍在 running 的 task。
  for (const pending of pendingTask.values()) {
    nodes.push({
      node_id: pending.nodeId,
      parent_node_id: "run",
      kind: "task",
      label: "子代理任务",
      status: "running",
      agent_name: pending.event.agent_name,
      agent_role: agentRole(pending.event.agent_name),
      depth: 0,
      started_at: pending.startedAt,
      tool_name: "task",
      raw_event_ids: [pending.event.event_id],
      chain_summary: "运行中…",
    });
  }

  return { run, events, nodes, context, todos };
}

// ── chain_summary 生成函数（前端版本，与后端 chain_summary.py 保持一致） ──

function runSummary(run: TraceRunSummary): string {
  const statusLabel = run.status === "completed"
    ? "完成"
    : run.status === "failed"
      ? "失败"
      : run.status === "awaiting_input"
        ? "等待输入"
        : run.status === "cancelled"
          ? "已取消"
          : "运行中";
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

function todoSummary(items: TraceTodoItem[]): string {
  if (!items.length) return "0 个任务";
  const active = items.find((item) => item.status === "in_progress")?.content;
  if (active) return `${items.length} 个任务，当前: ${active}`;
  return `${items.length} 个任务`;
}

// ── Skill 检测 ──

function skillTextContent(output: unknown): string | null {
  if (typeof output === "string") return output;
  if (output && typeof output === "object") {
    const obj = output as Record<string, unknown>;
    if (typeof obj.content === "string") return obj.content;
    if (typeof obj.text === "string") return obj.text;
  }
  return null;
}

function detectSkill(event: TraceLogEvent): string | null {
  if (event.tool_name !== "read_file" && event.tool_name !== "read") return null;
  const path = extractPath(event.tool_output);
  if (!path || !path.replace(/\/$/, "").endsWith("SKILL.md")) return null;
  const content = skillTextContent(event.tool_output);
  if (content) {
    const match = content.match(/^---\s*\n[\s\S]*?^name:\s*(.+)$/m);
    if (match) return match[1].trim();
  }
  return "unknown-skill";
}

// ── Todo 提取 ──

function findTodos(output: unknown): TraceTodoItem[] | null {
  if (!output || typeof output !== "object") return null;
  const obj = output as Record<string, unknown>;
  // 三种嵌套结构：output.todos / output.update.todos / output.args.todos
  if (Array.isArray(obj.todos)) return parseTodoItems(obj.todos);
  const update = obj.update;
  if (update && typeof update === "object" && Array.isArray((update as Record<string, unknown>).todos)) {
    return parseTodoItems((update as Record<string, unknown>).todos as unknown[]);
  }
  const args = obj.args;
  if (args && typeof args === "object" && Array.isArray((args as Record<string, unknown>).todos)) {
    return parseTodoItems((args as Record<string, unknown>).todos as unknown[]);
  }
  return null;
}

function parseTodoItems(raw: unknown[]): TraceTodoItem[] {
  return raw.map((item, index) => {
    if (!item || typeof item !== "object") {
      return { id: String(index + 1), content: String(item), status: "pending" as const };
    }
    const obj = item as Record<string, unknown>;
    const content = obj.content ?? obj.title;
    let status = obj.status;
    if (status !== "pending" && status !== "in_progress" && status !== "completed") {
      status = "pending";
    }
    const id = obj.id;
    return {
      id: id != null ? String(id) : String(index + 1),
      content: String(content),
      status: status as TraceTodoItem["status"],
    };
  });
}

// ── Tool error 提取 ──

function toolError(output: unknown): string | null {
  if (output && typeof output === "object") {
    const obj = output as Record<string, unknown>;
    if (obj.status === "error") {
      const content = obj.content;
      return content != null ? String(content) : "Tool returned error status.";
    }
  }
  return null;
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
  if (event.type === "run_end" || event.type === "run_error" || event.type === "run_cancelled") {
    return {
      ...run,
      status: event.status,
      ended_at: event.timestamp,
      duration_ms: event.duration_ms ?? run.duration_ms,
      event_count: event.sequence,
      error: event.error ?? run.error,
    };
  }
  if (event.type === "run_awaiting") {
    return { ...run, status: "awaiting_input", event_count: Math.max(run.event_count, event.sequence) };
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

function effectiveAgentName(agentName?: string | null): string | null | undefined {
  if (agentName && isEvaluationAgent(agentName)) return evaluationPrimaryAgentName(agentName);
  return agentName;
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
    const lastMessage = Array.isArray(messages) ? messages.findLast((message: unknown) => messageKind(message) === "ai") : null;
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
