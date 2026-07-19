/**
 * executionStore —— 创作对话执行编排（从 home.tsx 迁移）
 *
 * 职责：消息列表、SSE 流式消费、提交/停止/重试、会话消息存取。
 *
 * 跨 store 依赖：
 * - workspaceStore：activeThreadId / activeWorkspaceId / activeThread / createThread / updateThread
 * - traceStore：traceRuns / setTraceRuns / setTraceDetail / setActiveTraceId / setLiveTraceId
 *
 * 设计约束（来自设计文档 20260710_140000）：
 * - 高频更新（model_stream）用 RAF 批量同步（P1-A 先用直接更新，RAF 在 T2 后续优化）
 * - executionPhase 存 message（与 status 并存）—— P1-A 先保留原 status 逻辑，phase 在 P1-C 补
 * - reasoning 仅瞬态不持久化（activeReasoning）—— P2 补
 */
import { create } from "zustand";
import { toast } from "sonner";
import type { AskUserOption, ChatMessage, ScreenplayResponse, StreamEvent, ToolStatus, TraceLogEvent, TraceRunSummary } from "@/lib/types";
import { streamRequest } from "@/lib/stream";
import { appendLiveTraceEvent } from "@/lib/trace";
import { derivePhaseFromMessage } from "@/lib/execution-phase";
import {
  API_BASE_URL,
  apiFetch,
  createThread as createThreadRequest,
  updateThread as updateThreadRequest,
  trackCopy,
  trackRegenerate,
} from "@/lib/api";

// ── 心跳超时（与原 home.tsx 一致）──
const HEARTBEAT_TIMEOUT_MS = 45_000;

const initialAssistantMessage: ChatMessage = {
  role: "assistant",
  content: "先选择一个工作目录，再开启或恢复创作会话。",
};

// ── SSE 事件字段提取（从 home.tsx:86-133 迁移）──

function getToolName(event: StreamEvent) {
  return String(event.data.tool ?? "").trim() || "未知工具";
}

function getToolCallId(event: StreamEvent) {
  return String(event.data.call_id ?? "").trim();
}

function getToolParentKey(event: StreamEvent) {
  return String(event.data.parent_task_id ?? "").trim() || undefined;
}

function getSubagentName(event: StreamEvent) {
  const name = String(event.data.subagent_name ?? "").trim();
  return name || undefined;
}

function buildToolKey(toolName: string, callId: string, index: number) {
  return callId || `${toolName}-${index + 1}`;
}

function numOrNull(value: unknown): number | null {
  if (typeof value === "number") return value;
  if (typeof value === "string" && value.trim() && !Number.isNaN(Number(value))) return Number(value);
  return null;
}

function getTaskFocus(event: StreamEvent) {
  const data = event.data;
  return {
    subagentType: typeof data.subagent_type === "string" ? data.subagent_type : undefined,
    chapterIndex: numOrNull(data.chapter_index),
    totalChapters: numOrNull(data.total_chapters),
    iteration: numOrNull(data.iteration),
  };
}

function getWordCountPatch(event: StreamEvent): { wordCount?: number; chapterIndex?: number } {
  const data = event.data;
  const patch: { wordCount?: number; chapterIndex?: number } = {};
  const wc = numOrNull(data.word_count);
  if (wc != null) patch.wordCount = wc;
  const ci = numOrNull(data.chapter_index);
  if (ci != null) patch.chapterIndex = ci;
  return patch;
}

// ── 工具状态机（从 home.tsx:135-253 迁移）──

function upsertRunningTool(tools: ToolStatus[] | undefined, event: StreamEvent) {
  const toolName = getToolName(event);
  const callId = getToolCallId(event);
  const parentKey = getToolParentKey(event);
  const subagentName = getSubagentName(event);
  const focus = getTaskFocus(event);
  const nextTools = [...(tools ?? [])];

  if (callId) {
    const existingIndex = nextTools.findIndex((tool) => tool.key === callId);
    if (existingIndex >= 0) {
      nextTools[existingIndex] = { ...nextTools[existingIndex], name: toolName, status: "running", ...focus };
      return nextTools;
    }
    nextTools.push({ key: callId, name: toolName, status: "running", parentKey, subagentName, ...focus });
    return nextTools;
  }

  nextTools.push({ key: buildToolKey(toolName, "", nextTools.length), name: toolName, status: "running", parentKey, subagentName, ...focus });
  return nextTools;
}

function markToolComplete(tools: ToolStatus[] | undefined, event: StreamEvent) {
  const toolName = getToolName(event);
  const eventCallId = getToolCallId(event);
  const patch = getWordCountPatch(event);
  const nextTools = [...(tools ?? [])];
  const markDone = (tool: ToolStatus): ToolStatus => ({ ...tool, status: "done", ...patch });

  if (eventCallId) {
    for (let i = 0; i < nextTools.length; i++) {
      if (nextTools[i].key === eventCallId && nextTools[i].status === "running") {
        nextTools[i] = markDone(nextTools[i]);
        return nextTools;
      }
    }
  }
  for (let i = nextTools.length - 1; i >= 0; i--) {
    if (nextTools[i].name === toolName && nextTools[i].status === "running") {
      nextTools[i] = markDone(nextTools[i]);
      return nextTools;
    }
  }
  for (let i = nextTools.length - 1; i >= 0; i--) {
    if (nextTools[i].name === toolName) {
      nextTools[i] = markDone(nextTools[i]);
      return nextTools;
    }
  }
  nextTools.push({ key: buildToolKey(toolName, "", nextTools.length), name: toolName, status: "done", ...patch });
  return nextTools;
}

function markToolFailed(tools: ToolStatus[] | undefined, event: StreamEvent) {
  const toolName = getToolName(event);
  const eventCallId = getToolCallId(event);
  const nextTools = [...(tools ?? [])];
  const markFailed = (tool: ToolStatus): ToolStatus => ({ ...tool, status: "failed" });

  if (eventCallId) {
    for (let i = 0; i < nextTools.length; i++) {
      if (nextTools[i].key === eventCallId && nextTools[i].status === "running") {
        nextTools[i] = markFailed(nextTools[i]);
        return nextTools;
      }
    }
  }
  for (let i = nextTools.length - 1; i >= 0; i--) {
    if (nextTools[i].name === toolName && nextTools[i].status === "running") {
      nextTools[i] = markFailed(nextTools[i]);
      return nextTools;
    }
  }
  for (let i = nextTools.length - 1; i >= 0; i--) {
    if (nextTools[i].name === toolName) {
      nextTools[i] = markFailed(nextTools[i]);
      return nextTools;
    }
  }
  nextTools.push({ key: buildToolKey(toolName, "", nextTools.length), name: toolName, status: "failed" });
  return nextTools;
}

// ── trace run 辅助（从 home.tsx:255-316 迁移）──

function runFromTraceEvent(event: TraceLogEvent): TraceRunSummary {
  const input = event.input;
  if (!input || typeof input !== "object") {
    throw new Error("Trace run_start event is missing input metadata.");
  }
  const metadata = input as Record<string, unknown>;
  return {
    trace_id: event.trace_id,
    workspace_id: String(metadata.workspace_id ?? ""),
    thread_id: String(metadata.thread_id ?? ""),
    session_name: String(metadata.session_name ?? ""),
    workspace_path: "",
    endpoint: String(metadata.endpoint ?? "unknown"),
    status: event.status,
    started_at: event.timestamp,
    ended_at: null,
    duration_ms: null,
    event_count: event.sequence,
    path: "",
    error: null,
  };
}

function fallbackRunFromTraceEvent(event: TraceLogEvent, threadId: string, workspaceId: string, threadSessionName: string, threadWorkspacePath: string): TraceRunSummary {
  return {
    trace_id: event.trace_id,
    workspace_id: workspaceId,
    thread_id: threadId,
    session_name: threadSessionName,
    workspace_path: threadWorkspacePath,
    endpoint: "screenplay.generate.stream",
    status: event.status,
    started_at: event.timestamp,
    ended_at: null,
    duration_ms: null,
    event_count: event.sequence,
    path: "",
    error: event.error ?? null,
  };
}

function upsertTraceRun(runs: TraceRunSummary[], run: TraceRunSummary) {
  return [run, ...runs.filter((item) => item.trace_id !== run.trace_id)];
}

function updateTraceRunFromEvent(run: TraceRunSummary, event: TraceLogEvent): TraceRunSummary {
  if (event.type === "run_end" || event.type === "run_error") {
    return { ...run, status: event.status, ended_at: event.timestamp, duration_ms: event.duration_ms ?? run.duration_ms, event_count: event.sequence, error: event.error ?? run.error };
  }
  return { ...run, status: event.status === "failed" ? "failed" : run.status, event_count: Math.max(run.event_count, event.sequence) };
}

// ── 消息辅助 ──

function updateAssistantMessage(
  messages: ChatMessage[],
  assistantIdx: number,
  updater: (message: ChatMessage) => ChatMessage,
) {
  const assistant = messages[assistantIdx];
  if (!assistant || assistant.role !== "assistant") {
    throw new Error("Assistant message is missing.");
  }
  const next = [...messages];
  const updated = updater(assistant);
  // T17: 每次更新 assistant message 后自动派生 executionPhase
  next[assistantIdx] = { ...updated, executionPhase: derivePhaseFromMessage(updated) };
  return next;
}

function getSessionTitle(input: string) {
  return Array.from(input.trim()).slice(0, 15).join("");
}

// ── 跨 store 依赖接口（避免循环 import）──
// executionStore 需要读写 workspace/trace store 的部分状态，
// 用 getter 注入而不是直接 import，避免模块级循环依赖。

export interface ExecutionDeps {
  // workspace getters
  getActiveThreadId: () => string;
  getActiveWorkspaceId: () => string;
  getActiveThreadSessionName: () => string;
  getActiveThreadWorkspacePath: () => string;
  getActiveWorkspaceDomain: () => string;
  // workspace actions
  setActiveThreadId: (id: string) => void;
  setThreads: (updater: (current: any[]) => any[]) => void;
  addThread: (thread: any) => void;
  // trace actions
  getTraceRuns: () => any[];
  getActiveTraceId: () => string;
  // liveTraceId = 当前正在跑的 trace（区别于 activeTraceId = 面板里选中查看的那条）。
  // 停止/重试必须用 liveTraceId，否则用户在看历史 trace 时点停止，会停成已结束的旧 trace。
  getLiveTraceId: () => string;
  setTraceRuns: (updater: any) => void;
  setTraceDetail: (updater: any) => void;
  setActiveTraceId: (id: string) => void;
  setLiveTraceId: (id: string) => void;
}

// 全局依赖注入（由 home.tsx 在初始化时注入）
let deps: ExecutionDeps | null = null;
export function setExecutionDeps(d: ExecutionDeps) { deps = d; }
function requireDeps(): ExecutionDeps {
  if (!deps) throw new Error("ExecutionDeps not injected. Call setExecutionDeps first.");
  return deps;
}

// ── Store 类型 ──

interface ExecutionState {
  // ── 持久态（写 message）──
  messages: ChatMessage[];
  prompt: string;
  loading: boolean;
  result: ScreenplayResponse | null;

  // ── 瞬态（运行中，不持久）──
  activeReasoning: string; // P2: reasoning_stream 累积
  hasHistory: boolean; // T18: 当前会话是否已有历史交互（驱动记忆感开场白）

  // ── 内部 ref 等价物 ──
  streamReader: { read: () => Promise<{ done: boolean; value: Uint8Array | undefined }>; cancel: () => Promise<void> } | null;
  threadMessages: Map<string, ChatMessage[]>;

  // ── actions ──
  setPrompt: (prompt: string) => void;
  setMessages: (updater: ChatMessage[] | ((current: ChatMessage[]) => ChatMessage[])) => void;
  switchThread: (threadId: string) => void;
  loadThreadMessages: (threadId: string) => void;
  resetMessages: () => void;
  clearThreadMessages: () => void;
  submit: (promptText: string) => Promise<void>;
  resume: (resumeText: string) => Promise<void>;
  stop: () => void;
  retry: () => void;
  submitImage: (opts: { prompt: string } | { resume: import("@/lib/api").ImageReviewResume }) => Promise<void>;
}

export const useExecutionStore = create<ExecutionState>((set, get) => ({
  messages: [initialAssistantMessage],
  prompt: "",
  loading: false,
  result: null,
  activeReasoning: "",
  hasHistory: false,
  streamReader: null,
  threadMessages: new Map(),

  setPrompt: (prompt) => set({ prompt }),

  setMessages: (updater) =>
    set((state) => ({
      messages: typeof updater === "function" ? (updater as (c: ChatMessage[]) => ChatMessage[])(state.messages) : updater,
    })),

  switchThread: (threadId) => {
    const { threadMessages, messages } = get();
    const d = requireDeps();
    // 保存旧 thread 的消息
    const prevId = d.getActiveThreadId();
    if (prevId && prevId !== threadId) {
      threadMessages.set(prevId, messages);
    }
    // 加载新 thread 的消息
    const saved = threadMessages.get(threadId);
    set({ messages: saved || [initialAssistantMessage] });
    d.setActiveThreadId(threadId);
  },

  loadThreadMessages: (threadId) => {
    const { threadMessages } = get();
    const saved = threadMessages.get(threadId);
    set({ messages: saved || [initialAssistantMessage] });
  },

  resetMessages: () => set({ messages: [initialAssistantMessage] }),

  clearThreadMessages: () => {
    get().threadMessages.clear();
  },

  submit: async (promptText) => {
    await performSubmit(set, get, promptText);
  },

  resume: async (resumeText) => {
    await performSubmit(set, get, resumeText);
  },

  stop: () => {
    handleStopGeneration(set, get);
  },

  retry: () => {
    handleRetry(set, get);
  },

  submitImage: async (opts) => {
    await performImageStream(set, get, opts);
  },
}));

// ── 核心实现（从 home.tsx:1099-1586 迁移）──

// streamRequest 返回的 reader 类型
type StreamReader = { read: () => Promise<{ done: boolean; value: Uint8Array | undefined }>; cancel: () => Promise<void> };

async function performSubmit(
  set: (partial: Partial<ExecutionState> | ((s: ExecutionState) => Partial<ExecutionState>)) => void,
  get: () => ExecutionState,
  promptText: string,
) {
  const trimmedPrompt = promptText.trim();
  if (!trimmedPrompt || get().loading) return;
  const d = requireDeps();

  const messages = get().messages;
  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");
  const isResume = !!lastAssistant?.awaitingInput;
  const resumeTraceId = isResume ? lastAssistant?.traceId ?? null : null;

  // D4：resume 时立即清空上一条 awaitingInput
  const prevAwaitingInputIdx = isResume ? messages.lastIndexOf(lastAssistant!) : -1;
  if (prevAwaitingInputIdx >= 0) {
    set((state) => {
      const target = state.messages[prevAwaitingInputIdx];
      if (!target || target.role !== "assistant" || !target.awaitingInput) return {};
      const next = [...state.messages];
      next[prevAwaitingInputIdx] = { ...target, awaitingInput: undefined };
      return { messages: next };
    });
  }

  const activeWorkspaceId = d.getActiveThreadId() ? d.getActiveWorkspaceId() : "";
  if (!activeWorkspaceId) {
    toast.error("请先选择或创建一个工作目录。");
    return;
  }

  set({ loading: true, result: null });

  if (resumeTraceId) {
    d.setLiveTraceId(resumeTraceId);
    d.setTraceDetail((current: any) => {
      if (current?.run.trace_id === resumeTraceId) return current;
      const run = d.getTraceRuns().find((r: any) => r.trace_id === resumeTraceId);
      return run ? { run, events: [], nodes: [], context: [], todos: [] } : current;
    });
  } else {
    d.setLiveTraceId("");
    d.setTraceDetail(() => null);
  }

  // 保存当前会话消息
  const activeThreadId = d.getActiveThreadId();
  if (activeThreadId) {
    get().threadMessages.set(activeThreadId, get().messages);
  }

  // T18: 记忆感——检测当前会话是否已有历史交互（至少一轮 user→assistant 完成）
  // 用于 BootingView 选择记忆文案（"好的，接着上次的故事..."）
  const hasHistory = get().messages.some(
    (m) => m.role === "user",
  ) && get().messages.some(
    (m) => m.role === "assistant" && (m.status === "completed" || m.status === "failed" || m.status === "stopped"),
  );

  const assistantIdx = get().messages.length + 1;
  set((state) => ({
    messages: [
      ...state.messages,
      { role: "user", content: trimmedPrompt },
      {
        role: "assistant",
        content: "正在执行...",
        contentFormat: "markdown",
        tools: [],
        traceId: resumeTraceId ?? undefined,
      },
    ],
    prompt: "",
    hasHistory,
  }));

  set({ streamReader: null });

  try {
    let userMessageThreadId = activeThreadId;
    let shouldNameThread = d.getActiveThreadSessionName().startsWith("会话 ") ?? false;

    if (!userMessageThreadId) {
      const thread = await createThreadRequest(activeWorkspaceId, getSessionTitle(trimmedPrompt));
      userMessageThreadId = thread.thread_id;
      shouldNameThread = false;
      get().threadMessages.set(thread.thread_id, get().messages);
      d.setThreads((current: any[]) => [thread, ...current.filter((item: any) => item.thread_id !== thread.thread_id)]);
      d.setActiveThreadId(thread.thread_id);
    }

    const nextSessionName = shouldNameThread ? getSessionTitle(trimmedPrompt) : "";
    if (nextSessionName) {
      updateThreadRequest(userMessageThreadId, nextSessionName)
        .then((thread) => {
          d.setThreads((current: any[]) => current.map((item: any) => (item.thread_id === thread.thread_id ? thread : item)));
        })
        .catch((renameError) => {
          toast.error(renameError instanceof Error ? renameError.message : "无法更新会话名称。");
        });
    }

    const reader = (await streamRequest("/api/screenplay/generate/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: isResume
        ? { thread_id: userMessageThreadId, resume: trimmedPrompt, trace_id: resumeTraceId ?? undefined }
        : { thread_id: userMessageThreadId, prompt: trimmedPrompt },
    })) as StreamReader;
    set({ streamReader: reader });

    const decoder = new TextDecoder();
    let buffer = "";
    let streamedText = "";
    let reasoningText = ""; // T21: reasoning_stream 累积（瞬态，不写 message）
    let hasModelOutput = false;
    let finalData: ScreenplayResponse | null = null;

    while (true) {
      let heartbeatTimer: ReturnType<typeof setTimeout> | undefined;
      const heartbeatTimeout = new Promise<never>((_, reject) => {
        heartbeatTimer = setTimeout(() => reject(new Error("HEARTBEAT_TIMEOUT")), HEARTBEAT_TIMEOUT_MS);
      });

      let done = false;
      let value: Uint8Array | undefined;
      try {
        const chunk = (await Promise.race([reader.read(), heartbeatTimeout])) as { done: boolean; value: Uint8Array | undefined };
        done = chunk.done;
        value = chunk.value;
      } catch {
        reader.cancel().catch(() => {});
        throw new Error("HEARTBEAT_TIMEOUT");
      } finally {
        if (heartbeatTimer) clearTimeout(heartbeatTimer);
      }

      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      let eventType = "";
      let eventData = "";

      for (const line of lines) {
        if (line.startsWith("event: ")) {
          eventType = line.slice(7).trim();
        } else if (line.startsWith("data: ") && eventType) {
          eventData = line.slice(6).trim();
          try {
            const event = { type: eventType as StreamEvent["type"], data: JSON.parse(eventData) as Record<string, unknown> };

            if (event.type === "model_stream") {
              streamedText += String(event.data.content ?? "");
            } else if (event.type === "reasoning_stream") {
              // T21: 累积 reasoning token 到瞬态 activeReasoning（不写 message，不持久化）
              reasoningText += String(event.data.content ?? "");
              set({ activeReasoning: reasoningText });
            } else if (event.type === "trace_event") {
              const traceEvent = event.data as TraceLogEvent;
              if (traceEvent.type === "run_start") {
                set((state) => ({
                  messages: updateAssistantMessage(state.messages, assistantIdx, (message) => ({ ...message, traceId: traceEvent.trace_id })),
                }));
              }
              d.setTraceRuns((current: any) => {
                if (traceEvent.type === "run_start") return upsertTraceRun(current, runFromTraceEvent(traceEvent));
                return current.map((run: any) => (run.trace_id === traceEvent.trace_id ? updateTraceRunFromEvent(run, traceEvent) : run));
              });
              d.setTraceDetail((current: any) => {
                const fallbackRun = current?.run.trace_id === traceEvent.trace_id
                  ? current.run
                  : d.getTraceRuns().find((run: any) => run.trace_id === traceEvent.trace_id) ??
                    fallbackRunFromTraceEvent(traceEvent, userMessageThreadId, activeWorkspaceId, d.getActiveThreadSessionName(), d.getActiveThreadWorkspacePath());
                return appendLiveTraceEvent(current, traceEvent, fallbackRun);
              });
              d.setActiveTraceId(traceEvent.trace_id);
              d.setLiveTraceId(traceEvent.trace_id);
              if (traceEvent.type === "run_end" || traceEvent.type === "run_error") {
                d.setLiveTraceId("");
              }
            } else if (event.type === "trace_snapshot") {
              const detail = event.data as any;
              d.setTraceDetail(() => detail);
              d.setTraceRuns((current: any) => upsertTraceRun(current, detail.run));
              d.setActiveTraceId(detail.run.trace_id);
              d.setLiveTraceId(detail.run.status === "running" ? detail.run.trace_id : "");
            } else if (event.type === "model_output") {
              hasModelOutput = true;
            } else if (event.type === "tool_call") {
              set((state) => ({
                messages: updateAssistantMessage(state.messages, assistantIdx, (message) => ({ ...message, tools: upsertRunningTool(message.tools, event) })),
              }));
            } else if (event.type === "tool_output") {
              set((state) => ({
                messages: updateAssistantMessage(state.messages, assistantIdx, (message) => ({ ...message, tools: markToolComplete(message.tools, event) })),
              }));
            } else if (event.type === "tool_error") {
              set((state) => ({
                messages: updateAssistantMessage(state.messages, assistantIdx, (message) => ({ ...message, tools: markToolFailed(message.tools, event) })),
              }));
            } else if (event.type === "final") {
              finalData = event.data as ScreenplayResponse;
            } else if (event.type === "credit_exhausted") {
              const msg = (event.data as { message?: string })?.message ?? "积分耗尽";
              d.setLiveTraceId("");
              toast.error(msg);
              set((state) => ({
                messages: updateAssistantMessage(state.messages, assistantIdx, (message) => ({
                  ...message,
                  status: "failed",
                  content: `⚠️ ${msg}\n\n已创作的内容已保存，补充积分后可继续创作。`,
                  contentFormat: "markdown",
                })),
              }));
              break;
            } else if (event.type === "interrupt") {
              const iv = event.data as {
                kind?: string; question?: string; options?: AskUserOption[] | null; multi_select?: boolean; source?: string;
                round?: number; versions?: unknown[];
              };
              const interruptKind = iv.kind ?? "choice";
              set((state) => ({
                messages: updateAssistantMessage(state.messages, assistantIdx, (message) => ({
                  ...message,
                  content: interruptKind === "image_review"
                    ? `第 ${iv.round ?? "?"} 轮图像评审：3 版 6 图已生成，请打分`
                    : iv.question || "等待你的输入",
                  awaitingInput: {
                    kind: interruptKind, question: iv.question || "", options: iv.options ?? null,
                    multi_select: iv.multi_select ?? false, source: iv.source, round: iv.round, versions: iv.versions,
                  },
                })),
              }));
            }
          } catch {
            // partial JSON
          }
          eventType = "";
          eventData = "";
        }
      }

      set((state) => ({
        messages: updateAssistantMessage(state.messages, assistantIdx, (message) => {
          if (streamedText) return { ...message, content: streamedText, contentFormat: "markdown" };
          return message;
        }),
      }));
    }

    if (finalData) {
      set({ result: finalData });
      d.setThreads((current: any[]) =>
        current.map((thread: any) => (thread.thread_id === finalData!.thread_id ? { ...thread, updated_at: new Date().toISOString() } : thread)),
      );
      set((state) => ({
        messages: updateAssistantMessage(state.messages, assistantIdx, (message) => ({
          ...message,
          status: "completed",
          content: streamedText || finalData?.markdown?.trim() || `已生成《${finalData.session_name}》的故事材料，工作目录是 ${finalData.workspace_path}`,
          contentFormat: "markdown",
        })),
      }));
    }
  } catch (submitError) {
    const errMsg = submitError instanceof Error ? submitError.message : "";
    if (errMsg.includes("积分") || errMsg.includes("403") || errMsg.includes("冻结")) {
      d.setLiveTraceId("");
      toast.error("积分余额不足，账户已冻结。请联系管理员补充积分。");
      set((state) => ({
        messages: updateAssistantMessage(state.messages, assistantIdx, (message) => ({
          ...message, status: "failed", content: "⚠️ 积分余额不足，无法开始创作。请联系管理员补充积分。", contentFormat: "markdown",
        })),
      }));
      if (isResume) throw submitError;
      return;
    }
    if (submitError instanceof DOMException && submitError.name === "AbortError") {
      d.setLiveTraceId("");
      set((state) => ({
        messages: updateAssistantMessage(state.messages, assistantIdx, (message) => ({
          ...message, status: "stopped",
          content: message.content === "正在执行..." ? "已手动停止。" : `${message.content}\n\n已手动停止。`, contentFormat: "markdown",
        })),
      }));
      if (isResume) throw submitError;
      return;
    }

    if (submitError instanceof Error && submitError.message === "HEARTBEAT_TIMEOUT") {
      const heartbeatMessage = `连接已断开（超过 ${HEARTBEAT_TIMEOUT_MS / 1000} 秒未收到数据），请重试。`;
      d.setLiveTraceId("");
      toast.error(heartbeatMessage);
      set((state) => ({
        messages: updateAssistantMessage(state.messages, assistantIdx, (message) => ({
          ...message, status: "failed",
          content: message.content === "正在执行..." ? `⚠️ ${heartbeatMessage}` : `${message.content}\n\n⚠️ ${heartbeatMessage}`, contentFormat: "markdown",
        })),
      }));
      if (isResume) throw submitError;
      return;
    }

    toast.error(submitError instanceof Error ? submitError.message : "Unexpected request failure.");
    set((state) => ({
      messages: updateAssistantMessage(state.messages, assistantIdx, (message) => ({
        ...message, status: "failed",
        content: message.content === "正在执行..."
          ? "请求失败了。检查后端是否启动后，可以在同一个会话里重试。"
          : `${message.content}\n\n⚠️ 请求失败，可重试。`, contentFormat: "markdown",
      })),
    }));
    if (isResume) throw submitError;
  } finally {
    set({ streamReader: null, loading: false });
  }
}

function handleStopGeneration(set: any, get: () => ExecutionState) {
  const d = requireDeps();
  const activeThreadId = d.getActiveThreadId();
  const liveTraceId = d.getLiveTraceId();

  if (activeThreadId && liveTraceId) {
    apiFetch(`${API_BASE_URL}/api/screenplay/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: activeThreadId, trace_id: liveTraceId }),
    }).catch(() => {});
  }
  get().streamReader?.cancel().catch(() => {});
}

function handleRetry(set: any, get: () => ExecutionState) {
  const d = requireDeps();
  const liveTraceId = d.getLiveTraceId();
  if (liveTraceId) {
    trackRegenerate(liveTraceId);
  }
  const lastUser = [...get().messages].reverse().find((m) => m.role === "user");
  if (lastUser?.content) {
    void performSubmit(set, get, lastUser.content);
  }
}

async function performImageStream(
  set: (partial: Partial<ExecutionState> | ((s: ExecutionState) => Partial<ExecutionState>)) => void,
  get: () => ExecutionState,
  opts: { prompt: string } | { resume: import("@/lib/api").ImageReviewResume },
) {
  const d = requireDeps();
  const activeThreadId = d.getActiveThreadId();
  if (!activeThreadId || get().loading) return;
  const isResume = "resume" in opts;

  if (isResume) {
    const lastAssistant = [...get().messages].reverse().find((m) => m.role === "assistant");
    if (!lastAssistant?.awaitingInput) return;
    set((state) => {
      const idx = state.messages.findIndex((m) => m === lastAssistant);
      if (idx < 0) return {};
      const next = [...state.messages];
      next[idx] = { ...lastAssistant, awaitingInput: undefined };
      return { messages: next };
    });
  }

  set({ loading: true, streamReader: null });

  if (!isResume) {
    set((state) => ({ messages: [...state.messages, { role: "user", content: opts.prompt, contentFormat: "text" }] }));
  }
  set((state) => ({
    messages: [...state.messages, { role: "assistant", content: isResume ? "正在优化..." : "正在生成...", contentFormat: "markdown", tools: [] }],
  }));

  try {
    const body = isResume ? { thread_id: activeThreadId, prompt: "", resume: opts.resume } : { thread_id: activeThreadId, prompt: opts.prompt };
    const reader = (await streamRequest("/api/image/generate/stream", {
      method: "POST", headers: { "Content-Type": "application/json" }, body,
    })) as StreamReader;
    set({ streamReader: reader });

    const decoder = new TextDecoder();
    let buffer = "";
    let streamedText = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop() ?? "";

      for (const evt of events) {
        const lines = evt.split("\n");
        let eventType = "";
        let eventData = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) eventType = line.slice(7);
          else if (line.startsWith("data: ")) eventData += line.slice(6);
        }
        if (!eventType) continue;
        try {
          const event = { type: eventType, data: JSON.parse(eventData) };
          if (event.type === "model_stream") {
            streamedText += (event.data as { content?: string }).content ?? "";
            set((state) => ({
              messages: updateAssistantMessage(state.messages, state.messages.length - 1, (m) => ({ ...m, content: streamedText, contentFormat: "markdown" })),
            }));
          } else if (event.type === "interrupt") {
            const iv = event.data as { kind?: string; round?: number; versions?: unknown[]; question?: string; options?: AskUserOption[] | null; multi_select?: boolean; source?: string };
            const interruptKind = iv.kind ?? "choice";
            set((state) => ({
              messages: updateAssistantMessage(state.messages, state.messages.length - 1, (m) => ({
                ...m,
                content: interruptKind === "image_review" ? `第 ${iv.round ?? "?"} 轮图像评审：请打分` : iv.question || "等待输入",
                awaitingInput: {
                  kind: interruptKind, question: iv.question || "", options: iv.options ?? null,
                  multi_select: iv.multi_select ?? false, source: iv.source, round: iv.round, versions: iv.versions,
                } as typeof m.awaitingInput,
              })),
            }));
            return;
          } else if (event.type === "final") {
            const data = event.data as { content?: string };
            set((state) => ({
              messages: updateAssistantMessage(state.messages, state.messages.length - 1, (m) => ({ ...m, content: data.content ?? "完成", contentFormat: "markdown" })),
            }));
            return;
          } else if (event.type === "error") {
            toast.error((event.data as { error?: string }).error ?? "image stream 出错");
            return;
          }
        } catch {
          // partial JSON
        }
      }
    }
  } catch (err) {
    toast.error(err instanceof Error ? err.message : "image stream 失败");
  } finally {
    set({ streamReader: null, loading: false });
  }
}
