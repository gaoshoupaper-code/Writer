import type { TraceDetail, TraceNode, ToolStatus } from "./types";

// ── 阶段流数据契约（设计第八节）──
// 一次提交 = 一条 assistant message = 一个 trace run。
// stageFlow 从 traceDetail.nodes（task/agent 节点）+ message.tools（实时章节/字数）派生。

export type StageType = "storybuilding" | "detail-outline" | "writing" | "general";

export interface StageSubStep {
  id: string; // task 节点 node_id（稳定唯一 key）
  toolCallId: string | null; // task 的 tool_call_id，匹配 message.tools
  label: string; // "第3章" | "第2轮" | "任务N"
  status: "running" | "completed" | "failed";
  wordCount?: number | null; // D7: chapter 字数（仅 writing）
  summary?: string; // D4: task tool_output 截断
  durationMs?: number | null;
}

export interface Stage {
  id: string;
  type: StageType;
  label: string; // 故事构建/细纲规划/正文写作/辅助任务
  status: "running" | "completed" | "failed";
  iteration?: { current: number; total?: number }; // storybuilding 轮次
  subSteps: StageSubStep[];
  summary?: string; // 阶段整体摘要（最后 subStep）
  focusText?: string; // D6+D7: "正在写第3章·约800字"（仅 running）
  durationMs: number | null; // D8: 该阶段累计耗时
  agentTaskCount: number; // D9: 子代理任务数
  toolCallCount: number; // D9: 子代理内部工具调用数
  startedAt?: string;
  endedAt?: string;
}

export interface StageFlow {
  stages: Stage[];
  currentStageId: string | null;
  totalDurationMs: number | null;
  status: "running" | "completed" | "failed" | "stopped";
}

const STAGE_LABELS: Record<StageType, string> = {
  storybuilding: "故事构建",
  "detail-outline": "细纲规划",
  writing: "正文写作",
  general: "辅助任务",
};

const STAGE_ORDER: StageType[] = ["storybuilding", "detail-outline", "writing", "general"];

// 空态：无 trace 或无节点
const EMPTY_FLOW: StageFlow = { stages: [], currentStageId: null, totalDurationMs: null, status: "completed" };

/**
 * 从 traceDetail + message.tools 派生阶段流（D1/D2 纯派生）。
 *
 * 三源合并：
 * - traceDetail 的 task 节点（D4 新建）→ subStep 骨架、状态、tool_output 摘要、时长
 * - traceDetail 的 agent 节点（按 -subagent 后缀映射 type）→ toolCallCount（D9）+ 历史回放 type 时序推断
 * - message.tools（SSE tool_call 扩展字段）→ 实时 subagentType / 章节号 / 字数（D6/D7）
 *
 * 历史回放（tools 空）：按 task 区间时序匹配 agent 节点推断 type，缺失降级为 "general"。
 */
export function projectStageFlow(detail: TraceDetail | null, tools: ToolStatus[]): StageFlow {
  if (!detail || detail.nodes.length === 0) return EMPTY_FLOW;

  const nodes = detail.nodes;

  // 1. task 节点 = subStep 骨架；agent 实例节点 = type 推断 + toolCallCount 来源
  const taskNodes = nodes.filter((n) => n.kind === "task");
  const agentNodes = nodes
    .filter((n) => n.kind === "agent" && n.agent_role === "subagent")
    .sort((a, b) => (a.started_at ?? "").localeCompare(b.started_at ?? ""));

  const agentInstanceType = new Map<string, StageType>();
  for (const a of agentNodes) {
    const t = agentNameToStageType(a.agent_name);
    if (t) agentInstanceType.set(a.node_id, t);
  }

  // 2. toolCallCount per type：tool/skill 节点的 parent（agent 实例）归属哪个 type
  const toolCountPerType = new Map<StageType, number>();
  for (const n of nodes) {
    if (n.kind !== "tool" && n.kind !== "skill") continue;
    const t = n.parent_node_id ? agentInstanceType.get(n.parent_node_id) : undefined;
    if (t) toolCountPerType.set(t, (toolCountPerType.get(t) ?? 0) + 1);
  }

  // 3. tool_call_id → ToolStatus（实时 type / 章节 / 字数）
  const toolByCallId = new Map<string, ToolStatus>();
  for (const t of tools) {
    if (t.key) toolByCallId.set(t.key, t);
  }

  // 4. 给每个 task 节点定 type：优先 tools[subagentType]，降级时序匹配 agent 节点
  const usedAgents = new Set<string>();
  const typedTasks: Array<{ node: TraceNode; type: StageType; callId: string | null }> = [];

  for (const taskNode of taskNodes) {
    const callId = taskNodeIdToCallId(taskNode.node_id);
    const tool = callId ? toolByCallId.get(callId) : undefined;
    let type: StageType | null = tool?.subagentType ? toStageType(tool.subagentType) : null;

    if (!type) {
      // 历史回放降级：started_at 落在 task 区间内的首个未占用 agent 实例决定 type
      const start = taskNode.started_at ?? "";
      const end = taskNode.ended_at ?? start;
      for (const a of agentNodes) {
        if (usedAgents.has(a.node_id)) continue;
        const at = a.started_at ?? "";
        if (at >= start && at <= end) {
          const t = agentInstanceType.get(a.node_id);
          if (t) {
            usedAgents.add(a.node_id);
            type = t;
            break;
          }
        }
      }
    }
    typedTasks.push({ node: taskNode, type: type ?? "general", callId });
  }

  // 5. 按 type 分组成 Stage，保持首次出现顺序
  const stageOrder: StageType[] = [];
  const stageTasks = new Map<StageType, typeof typedTasks>();
  for (const tt of typedTasks) {
    if (!stageTasks.has(tt.type)) {
      stageTasks.set(tt.type, []);
      stageOrder.push(tt.type);
    }
    stageTasks.get(tt.type)!.push(tt);
  }

  // 6. 组装 Stage
  const stages: Stage[] = stageOrder.map((type) => buildStage(type, stageTasks.get(type)!, toolByCallId, toolCountPerType.get(type) ?? 0));

  // 7. currentStageId / status / totalDuration
  const runningStage = stages.find((s) => s.status === "running");
  const currentStageId = runningStage?.id ?? stages[stages.length - 1]?.id ?? null;
  const totalDurationMs = sumDuration(stages.map((s) => s.durationMs));
  const status = deriveFlowStatus(detail, stages);

  return { stages, currentStageId, totalDurationMs, status };
}

function buildStage(
  type: StageType,
  tasks: Array<{ node: TraceNode; type: StageType; callId: string | null }>,
  toolByCallId: Map<string, ToolStatus>,
  toolCallCount: number,
): Stage {
  const subSteps: StageSubStep[] = tasks.map((tt, idx) => {
    const tool = tt.callId ? toolByCallId.get(tt.callId) : undefined;
    const chapterIndex = tool?.chapterIndex ?? null;
    return {
      id: tt.node.node_id,
      toolCallId: tt.callId,
      label: subStepLabel(type, idx, chapterIndex),
      status: nodeStatusToStep(tt.node),
      wordCount: type === "writing" ? (tool?.wordCount ?? null) : undefined,
      summary: tt.node.chain_summary ?? undefined,
      durationMs: tt.node.duration_ms ?? null,
    };
  });

  const status = aggregateStatus(subSteps.map((s) => s.status));
  const startedAt = tasks[0]?.node.started_at ?? undefined;
  const endedAt = tasks[tasks.length - 1]?.node.ended_at ?? undefined;

  const stage: Stage = {
    id: `stage:${type}`,
    type,
    label: STAGE_LABELS[type],
    status,
    subSteps,
    summary: subSteps[subSteps.length - 1]?.summary,
    durationMs: sumDuration(tasks.map((t) => t.node.duration_ms ?? null)),
    agentTaskCount: subSteps.length,
    toolCallCount,
    startedAt,
    endedAt,
  };

  if (type === "storybuilding" && subSteps.length > 0) {
    stage.iteration = { current: subSteps.length };
  }
  stage.focusText = buildFocusText(stage);
  return stage;
}

// ── 辅助 ──

function agentNameToStageType(agentName?: string | null): StageType | null {
  if (!agentName) return null;
  const name = agentName.replace(/-subagent$/, "");
  return STAGE_ORDER.includes(name as StageType) ? (name as StageType) : null;
}

function toStageType(subagentType: string): StageType | null {
  return STAGE_ORDER.includes(subagentType as StageType) ? (subagentType as StageType) : null;
}

function taskNodeIdToCallId(nodeId: string): string | null {
  return nodeId.startsWith("task:") ? nodeId.slice(5) : null;
}

function nodeStatusToStep(node: TraceNode): "running" | "completed" | "failed" {
  if (node.status === "failed") return "failed";
  if (node.status === "running") return "running";
  return "completed";
}

function aggregateStatus(statuses: Array<"running" | "completed" | "failed">): "running" | "completed" | "failed" {
  if (statuses.includes("running")) return "running";
  if (statuses.includes("failed")) return "failed";
  return "completed";
}

function subStepLabel(type: StageType, idx: number, chapterIndex: number | null): string {
  if (type === "writing") return chapterIndex ? `第 ${chapterIndex} 章` : `任务 ${idx + 1}`;
  if (type === "storybuilding") return `第 ${idx + 1} 轮`;
  return `任务 ${idx + 1}`;
}

function sumDuration(durations: Array<number | null | undefined>): number | null {
  let sum = 0;
  let has = false;
  for (const d of durations) {
    if (d != null) {
      sum += d;
      has = true;
    }
  }
  return has ? sum : null;
}

function buildFocusText(stage: Stage): string | undefined {
  if (stage.status !== "running") return undefined;
  if (stage.type === "writing") {
    const step = stage.subSteps.find((s) => s.status === "running") ?? stage.subSteps[stage.subSteps.length - 1];
    if (!step) return "正在生成正文";
    const idx = extractChapterFromLabel(step.label) ?? stage.subSteps.indexOf(step) + 1;
    return step.wordCount ? `正在写第 ${idx} 章·约 ${step.wordCount} 字` : `正在写第 ${idx} 章`;
  }
  if (stage.type === "storybuilding") {
    return `正在构建故事（第 ${stage.iteration?.current ?? stage.subSteps.length} 轮）`;
  }
  if (stage.type === "detail-outline") return "正在规划章节细纲";
  return "正在执行辅助任务";
}

function extractChapterFromLabel(label: string): number | null {
  const m = label.match(/第\s*(\d+)\s*章/);
  return m ? Number(m[1]) : null;
}

function deriveFlowStatus(detail: TraceDetail, stages: Stage[]): StageFlow["status"] {
  if (detail.run.status === "failed") return "failed";
  if (stages.some((s) => s.status === "running")) return "running";
  if (detail.run.status === "completed") return "completed";
  return "running";
}
