"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { AppShell } from "../components/workspace/AppShell";
import { CharactersPanel } from "../components/workspace/CharactersPanel";
import { ChatPanel } from "../components/workspace/ChatPanel";
import { ConfirmDialog } from "../components/workspace/ConfirmDialog";
import { DetailOutlinePanel } from "../components/workspace/DetailOutlinePanel";
import { NovelPanel } from "../components/workspace/NovelPanel";
import { ScriptPanel } from "../components/workspace/ScriptPanel";
import { StorylinePanel } from "../components/workspace/StorylinePanel";
import { Sidebar } from "../components/workspace/Sidebar";
import { StyleModal } from "../components/workspace/StyleModal";
import { TopBar } from "../components/workspace/TopBar";
import { TracePanel } from "../components/workspace/TracePanel";
import { WorldviewPanel } from "../components/workspace/WorldviewPanel";
import {
  API_BASE_URL,
  apiFetch,
  activateStyle as activateStyleRequest,
  createStyle as createStyleRequest,
  createThread as createThreadRequest,
  createWorkspace as createWorkspaceRequest,
  deleteStyle as deleteStyleRequest,
  deleteThread as deleteThreadRequest,
  deleteTrace as deleteTraceRequest,
  deleteWorkspace as deleteWorkspaceRequest,
  fetchInit,
  fetchMeOrNull,
  fetchStyles as fetchStylesRequest,
  fetchThreadTraces,
  fetchThreads,
  fetchTraceDetail,
  fetchWorkspaceBootstrap,
  fetchWorkspaceCharacters,
  fetchWorkspaceDetailOutline,
  fetchWorkspaceNovel,
  fetchWorkspaceOutline,
  fetchWorkspaces,
  logout as logoutRequest,
  optimizeStyle as optimizeStyleRequest,
  updateStyle as updateStyleRequest,
  updateThread as updateThreadRequest,
  trackCopy,
  trackRegenerate,
  workspaceNovelPdfUrl,
  workspaceNovelWordUrl,
} from "../lib/api";
import { appendLiveTraceEvent } from "../lib/trace";
import { projectStageFlow } from "../lib/stage";
import { usePanelPolling } from "../lib/usePanelPolling";
import type { StageFlow } from "../lib/stage";
import type {
  AskUserOption,
  CharacterMarkdownFile,
  ChatMessage,
  DetailOutlineChapter,
  NovelChapter,
  ScreenplayResponse,
  Style,
  StreamEvent,
  ThreadSummary,
  ToolStatus,
  TraceDetail,
  TraceLogEvent,
  TraceRunSummary,
  StorylineEntry,
  WorkspacePanel,
  WorkspaceSummary,
} from "../lib/types";

type ThemeMode = "light" | "dark";

const initialPrompt = "";
const themeStorageKey = "writer-theme";
// 心跳超时：后端 SSE 每 15s 发一次 : ping，若超过该阈值仍收不到任何字节，即判定
// 连接已被 cloudflared/Cloudflare 静默掐断（此时 read() 因 TCP 半关闭会无限阻塞）。
const HEARTBEAT_TIMEOUT_MS = 45_000;
const initialAssistantMessage: ChatMessage = {
  role: "assistant",
  content: "先选择一个工作目录，再开启或恢复创作会话。",
};

function getToolName(event: StreamEvent) {
  const toolName = String(event.data.tool ?? "").trim();
  return toolName || "未知工具";
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

// P1 扩展字段提取：从 SSE tool_call/tool_output payload 取章节号/字数/轮次等焦点信息（D6/D7）
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

function upsertRunningTool(tools: ToolStatus[] | undefined, event: StreamEvent) {
  const toolName = getToolName(event);
  const callId = getToolCallId(event);
  const parentKey = getToolParentKey(event);
  const subagentName = getSubagentName(event);
  const focus = getTaskFocus(event);
  const nextTools = [...(tools ?? [])];
  const lookupKey = callId;

  if (lookupKey) {
    const existingIndex = nextTools.findIndex((tool) => tool.key === lookupKey);
    if (existingIndex >= 0) {
      nextTools[existingIndex] = {
        ...nextTools[existingIndex],
        name: toolName,
        status: "running",
        ...focus,
      };
      return nextTools;
    }

    nextTools.push({
      key: lookupKey,
      name: toolName,
      status: "running",
      parentKey,
      subagentName,
      ...focus,
    });
    return nextTools;
  }

  nextTools.push({
    key: buildToolKey(toolName, "", nextTools.length),
    name: toolName,
    status: "running",
    parentKey,
    subagentName,
    ...focus,
  });
  return nextTools;
}

function markToolComplete(tools: ToolStatus[] | undefined, event: StreamEvent) {
  const toolName = getToolName(event);
  const eventCallId = getToolCallId(event);
  const patch = getWordCountPatch(event);
  const nextTools = [...(tools ?? [])];
  const markDone = (tool: ToolStatus): ToolStatus => ({ ...tool, status: "done", ...patch });

  if (eventCallId) {
    for (let index = 0; index < nextTools.length; index += 1) {
      if (nextTools[index].key === eventCallId && nextTools[index].status === "running") {
        nextTools[index] = markDone(nextTools[index]);
        return nextTools;
      }
    }
  }

  for (let index = nextTools.length - 1; index >= 0; index -= 1) {
    if (nextTools[index].name === toolName && nextTools[index].status === "running") {
      nextTools[index] = markDone(nextTools[index]);
      return nextTools;
    }
  }

  for (let index = nextTools.length - 1; index >= 0; index -= 1) {
    if (nextTools[index].name === toolName) {
      nextTools[index] = markDone(nextTools[index]);
      return nextTools;
    }
  }

  nextTools.push({
    key: buildToolKey(toolName, "", nextTools.length),
    name: toolName,
    status: "done",
    ...patch,
  });
  return nextTools;
}

// S4：单工具失败（tool_error SSE）→ 标 failed。匹配逻辑与 markToolComplete 一致（call_id → name 回退）
function markToolFailed(tools: ToolStatus[] | undefined, event: StreamEvent) {
  const toolName = getToolName(event);
  const eventCallId = getToolCallId(event);
  const nextTools = [...(tools ?? [])];
  const markFailed = (tool: ToolStatus): ToolStatus => ({ ...tool, status: "failed" });

  if (eventCallId) {
    for (let index = 0; index < nextTools.length; index += 1) {
      if (nextTools[index].key === eventCallId && nextTools[index].status === "running") {
        nextTools[index] = markFailed(nextTools[index]);
        return nextTools;
      }
    }
  }

  for (let index = nextTools.length - 1; index >= 0; index -= 1) {
    if (nextTools[index].name === toolName && nextTools[index].status === "running") {
      nextTools[index] = markFailed(nextTools[index]);
      return nextTools;
    }
  }

  for (let index = nextTools.length - 1; index >= 0; index -= 1) {
    if (nextTools[index].name === toolName) {
      nextTools[index] = markFailed(nextTools[index]);
      return nextTools;
    }
  }

  nextTools.push({
    key: buildToolKey(toolName, "", nextTools.length),
    name: toolName,
    status: "failed",
  });
  return nextTools;
}

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

function fallbackRunFromTraceEvent(event: TraceLogEvent, thread: ThreadSummary | null, workspaceId: string): TraceRunSummary {
  return {
    trace_id: event.trace_id,
    workspace_id: thread?.workspace_id || workspaceId,
    thread_id: thread?.thread_id || "",
    session_name: thread?.session_name || "",
    workspace_path: thread?.workspace_path || "",
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
    return {
      ...run,
      status: event.status,
      ended_at: event.timestamp,
      duration_ms: event.duration_ms ?? run.duration_ms,
      event_count: event.sequence,
      error: event.error ?? run.error,
    };
  }
  return {
    ...run,
    status: event.status === "failed" ? "failed" : run.status,
    event_count: Math.max(run.event_count, event.sequence),
  };
}

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
  next[assistantIdx] = updater(assistant);
  return next;
}

function getSessionTitle(input: string) {
  return Array.from(input.trim()).slice(0, 15).join("");
}

export default function Home() {
  const [activePanel, setActivePanel] = useState<WorkspacePanel>("chat");
  const [prompt, setPrompt] = useState(initialPrompt);
  const [messages, setMessages] = useState<ChatMessage[]>([initialAssistantMessage]);
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState("");
  const [activeThreadId, setActiveThreadId] = useState("");
  const [newWorkspaceName, setNewWorkspaceName] = useState("失忆编剧大纲");
  const [result, setResult] = useState<ScreenplayResponse | null>(null);
  const [outlineMarkdown, setOutlineMarkdown] = useState("");
  const [outlineLoading, setOutlineLoading] = useState(false);
  const [detailOutlineChapters, setDetailOutlineChapters] = useState<DetailOutlineChapter[]>([]);
  const [detailOutlineLoading, setDetailOutlineLoading] = useState(false);
  const [activeDetailChapterFilename, setActiveDetailChapterFilename] = useState("");
  const [novelChapters, setNovelChapters] = useState<NovelChapter[]>([]);
  const [activeNovelFilename, setActiveNovelFilename] = useState("");
  const [novelLoading, setNovelLoading] = useState(false);
  const [characters, setCharacters] = useState<CharacterMarkdownFile[]>([]);
  const [charactersLoading, setCharactersLoading] = useState(false);
  const [activeCharacterFilename, setActiveCharacterFilename] = useState("");
  const [worldviewMarkdown, setWorldviewMarkdown] = useState("");
  const [worldviewLoading, setWorldviewLoading] = useState(false);
  const [storylineMarkdown, setStorylineMarkdown] = useState("");
  const [storylineEntries, setStorylineEntries] = useState<StorylineEntry[]>([]);
  const [activeStorylineFilename, setActiveStorylineFilename] = useState("");
  const [outlineTab, setOutlineTab] = useState<"outline" | "storyline">("outline");
  const [traceRuns, setTraceRuns] = useState<TraceRunSummary[]>([]);
  const [activeTraceId, setActiveTraceId] = useState("");
  const [liveTraceId, setLiveTraceId] = useState("");
  const [traceDetail, setTraceDetail] = useState<TraceDetail | null>(null);
  const [historyDetails, setHistoryDetails] = useState<Map<string, TraceDetail>>(new Map());
  const [traceLoading, setTraceLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  // 工作区 bootstrap（初次加载/切换）进行中标志：轮询 Hook 期间会跳过写 state，
  // 让 bootstrap 作为权威源，避免切换工作区时的内容状态闪烁。
  const [bootstrapping, setBootstrapping] = useState(false);
  const [creatingWorkspace, setCreatingWorkspace] = useState(false);
  const [creatingThread, setCreatingThread] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deletingTraceId, setDeletingTraceId] = useState("");
  const [deletingWorkspace, setDeletingWorkspace] = useState(false);
  const [theme, setTheme] = useState<ThemeMode>("light");
  const [themeReady, setThemeReady] = useState(false);
  // 多用户：登录态 + 是否已填 API Key
  const [authChecked, setAuthChecked] = useState(false);
  const [authUser, setAuthUser] = useState<{ username: string; is_admin: boolean } | null>(null);
  const [hasApiKey, setHasApiKey] = useState(false);
  const [workspaceCreateOpen, setWorkspaceCreateOpen] = useState(false);
  const [newWorkspaceDomain, setNewWorkspaceDomain] = useState<"writing" | "image">("writing");
  const [workspaceDeleteOpen, setWorkspaceDeleteOpen] = useState(false);
  const [pendingDeleteWorkspaceId, setPendingDeleteWorkspaceId] = useState("");
  const [sessionMenuOpen, setSessionMenuOpen] = useState(false);
  const [styles, setStyles] = useState<Style[]>([]);
  const [styleModalOpen, setStyleModalOpen] = useState(false);
  const [creatingStyle, setCreatingStyle] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const threadMessagesRef = useRef<Map<string, ChatMessage[]>>(new Map());
  const messagesRef = useRef<ChatMessage[]>(messages);
  messagesRef.current = messages;

  useEffect(() => {
    setTheme(window.localStorage.getItem(themeStorageKey) === "dark" ? "dark" : "light");
    setThemeReady(true);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    if (themeReady) {
      window.localStorage.setItem(themeStorageKey, theme);
    }
  }, [theme, themeReady]);

  // ── 路由守卫：未登录跳 /login ──
  useEffect(() => {
    let ignore = false;
    (async () => {
      const me = await fetchMeOrNull();
      if (ignore) return;
      if (!me) {
        window.location.href = "/login";
        return;
      }
      setAuthUser({ username: me.username, is_admin: me.is_admin });
      setHasApiKey(me.has_api_key);
      setAuthChecked(true);
    })();
    return () => { ignore = true; };
  }, []);

  async function handleLogout() {
    try {
      await logoutRequest();
    } catch {
      /* ignore */
    }
    window.location.href = "/login";
  }

  // ── 首次加载：2 个请求完成全部初始化 ──
  // ⚠️ 不能用 ref 提前 return 来"保证只跑一次"。React StrictMode（Next.js dev 默认
  // 开启）会对 mount 的 effect 先执行一遍 setup→cleanup→setup：第一次 setup 启动的
  // 异步 bootstrap() 会被 cleanup 设置的 ignore 丢弃，而第二次 setup 又被 ref 拦掉
  // 不重跑——结果 bootstrap 数据永远写不进 state，刷新后所有内容面板（大纲/人物/细纲/
  // 世界观/正文）全部空白。正确做法：只用 ignore 防竞态，StrictMode 下第二次 setup
  // 会重新发起请求并成功写入（dev 多一次请求，可接受）。
  useEffect(() => {
    let ignore = false;

    async function bootstrap() {
      try {
        setBootstrapping(true);
        // 请求 1：workspaces + styles
        const { workspaces: ws, styles: st } = await fetchInit();
        if (ignore) return;
        setWorkspaces(ws);
        setStyles(st);

        const firstWorkspaceId = ws[0]?.workspace_id || "";
        setActiveWorkspaceId(firstWorkspaceId);
        if (!firstWorkspaceId) return;

        // 请求 2：选中工作区的全部面板数据
        const data = await fetchWorkspaceBootstrap(firstWorkspaceId);
        if (ignore) return;
        setThreads(data.threads);
        setActiveThreadId(data.threads[0]?.thread_id || "");
        if (data.outline) setOutlineMarkdown(data.outline.markdown);
        if (data.storyline) {
          setStorylineMarkdown(data.storyline.index_markdown);
          setStorylineEntries(data.storyline.entries);
          setActiveStorylineFilename(data.storyline.entries[0]?.filename || "");
        }
        if (data.worldview) setWorldviewMarkdown(data.worldview.markdown);
        if (data.detail_outline) {
          setDetailOutlineChapters(data.detail_outline.chapters);
          setActiveDetailChapterFilename(
            data.detail_outline.chapters[0]?.filename || "",
          );
        }
        if (data.characters) {
          setCharacters(data.characters.characters);
          setActiveCharacterFilename(
            data.characters.characters[0]?.filename || "",
          );
        }
        if (data.novel) {
          setNovelChapters(data.novel.chapters);
          setActiveNovelFilename(data.novel.chapters[0]?.filename || "");
        }
        setOutlineLoading(false);
        setDetailOutlineLoading(false);
        setCharactersLoading(false);
        setNovelLoading(false);
        setWorldviewLoading(false);
      } catch (err) {
        if (!ignore) {
          toast.error(err instanceof Error ? err.message : "初始化加载失败。");
        }
      } finally {
        if (!ignore) {
          setOutlineLoading(false);
          setDetailOutlineLoading(false);
          setCharactersLoading(false);
          setNovelLoading(false);
          setWorldviewLoading(false);
          setBootstrapping(false);
        }
      }
    }

    setOutlineLoading(true);
    setDetailOutlineLoading(true);
    setCharactersLoading(true);
    setNovelLoading(true);
    setWorldviewLoading(true);
    bootstrap();

    return () => { ignore = true; };
  }, []);

  // ── 后续切换工作区：用 bootstrap 接口替代 5 个单独请求 ──
  const initialBootDone = useRef(false);
  const skipNextBootstrap = useRef(false);

  useEffect(() => {
    if (!activeWorkspaceId) return;

    // 跳过首次加载（上面已经处理了）
    if (!initialBootDone.current) {
      initialBootDone.current = true;
      return;
    }

    // 跳过新创建的空工作区（数据已在 handleCreateWorkspace 中预设为空）
    if (skipNextBootstrap.current) {
      skipNextBootstrap.current = false;
      return;
    }

    let ignore = false;

    async function loadWorkspace() {
      setBootstrapping(true);
      setOutlineLoading(true);
      setDetailOutlineLoading(true);
      setCharactersLoading(true);
      setNovelLoading(true);
      setWorldviewLoading(true);

      try {
        const bootData = await fetchWorkspaceBootstrap(activeWorkspaceId);
        if (ignore) return;
        setThreads(bootData.threads);
        setActiveThreadId(
          bootData.threads.some((t) => t.thread_id === activeThreadId)
            ? activeThreadId
            : bootData.threads[0]?.thread_id || "",
        );
        if (bootData.outline) setOutlineMarkdown(bootData.outline.markdown);
        else setOutlineMarkdown("");
        if (bootData.storyline) {
          setStorylineMarkdown(bootData.storyline.index_markdown);
          setStorylineEntries(bootData.storyline.entries);
          setActiveStorylineFilename((cur) =>
            bootData.storyline!.entries.some((e) => e.filename === cur) ? cur : bootData.storyline!.entries[0]?.filename || "",
          );
        } else {
          setStorylineMarkdown("");
          setStorylineEntries([]);
          setActiveStorylineFilename("");
        }
        if (bootData.worldview) setWorldviewMarkdown(bootData.worldview.markdown);
        else setWorldviewMarkdown("");
        if (bootData.detail_outline) {
          setDetailOutlineChapters(bootData.detail_outline.chapters);
          setActiveDetailChapterFilename(
            bootData.detail_outline.chapters.some((c) => c.filename === activeDetailChapterFilename)
              ? activeDetailChapterFilename
              : bootData.detail_outline.chapters[0]?.filename || "",
          );
        } else {
          setDetailOutlineChapters([]);
          setActiveDetailChapterFilename("");
        }
        if (bootData.characters) {
          setCharacters(bootData.characters.characters);
          setActiveCharacterFilename(
            bootData.characters.characters.some((c) => c.filename === activeCharacterFilename)
              ? activeCharacterFilename
              : bootData.characters.characters[0]?.filename || "",
          );
        } else {
          setCharacters([]);
          setActiveCharacterFilename("");
        }
        if (bootData.novel) {
          setNovelChapters(bootData.novel.chapters);
          setActiveNovelFilename((cur) =>
            bootData.novel!.chapters.some((c) => c.filename === cur) ? cur : bootData.novel!.chapters[0]?.filename || "",
          );
        } else {
          setNovelChapters([]);
          setActiveNovelFilename("");
        }
      } catch (err) {
        if (!ignore) {
          toast.error(err instanceof Error ? err.message : "无法加载工作区数据。");
        }
      } finally {
        if (!ignore) {
          setOutlineLoading(false);
          setDetailOutlineLoading(false);
          setCharactersLoading(false);
          setNovelLoading(false);
          setWorldviewLoading(false);
          setBootstrapping(false);
        }
      }
    }

    loadWorkspace();

    return () => { ignore = true; };
  }, [activeWorkspaceId]);

  const activeWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.workspace_id === activeWorkspaceId) ?? null,
    [activeWorkspaceId, workspaces],
  );

  const activeStyleName = useMemo(() => {
    const activeStyleId = activeWorkspace?.active_style_id;
    if (!activeStyleId) return null;
    return styles.find((s) => s.style_id === activeStyleId)?.name ?? null;
  }, [activeWorkspace?.active_style_id, styles]);

  const activeThread = useMemo(
    () => threads.find((thread) => thread.thread_id === activeThreadId) ?? null,
    [activeThreadId, threads],
  );

  // D2: 当前 trace 的 stageFlow（从 traceDetail + 当前 assistant message 的 tools 派生）
  const stageFlow = useMemo<StageFlow | null>(() => {
    if (!traceDetail) return null;
    const owner = messages.findLast((m) => m.role === "assistant" && m.traceId === traceDetail.run.trace_id);
    return projectStageFlow(traceDetail, owner?.tools ?? []);
  }, [traceDetail, messages]);

  // 每条 message 对应的 stageFlow：当前 trace 命中用实时 stageFlow，历史 trace 用 historyDetails 派生
  const stageFlows = useMemo<(StageFlow | null)[]>(
    () =>
      messages.map((m) => {
        if (!m.traceId) return null;
        if (m.traceId === traceDetail?.run.trace_id) return stageFlow;
        const histDetail = historyDetails.get(m.traceId);
        return histDetail ? projectStageFlow(histDetail, m.tools ?? []) : null;
      }),
    [messages, traceDetail, stageFlow, historyDetails],
  );

  const prevThreadIdRef = useRef(activeThreadId);

  // 当切换工作区时，保存当前会话的消息
  useEffect(() => {
    const prevId = prevThreadIdRef.current;
    if (prevId && prevId !== activeThreadId) {
      threadMessagesRef.current.set(prevId, messagesRef.current);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeWorkspaceId]);

  // 当切换会话时，加载该会话的消息
  useEffect(() => {
    const prevId = prevThreadIdRef.current;
    if (prevId === activeThreadId) return;

    if (activeThreadId) {
      const saved = threadMessagesRef.current.get(activeThreadId);
      setMessages(saved || [initialAssistantMessage]);
    } else {
      setMessages([initialAssistantMessage]);
    }

    prevThreadIdRef.current = activeThreadId;
  }, [activeThreadId]);

  useEffect(() => {
    if (!activeThreadId) {
      setTraceRuns([]);
      setActiveTraceId("");
      setLiveTraceId("");
      setTraceDetail(null);
      return;
    }

    let ignore = false;
    setTraceLoading(true);

    async function loadTraceRuns() {
      try {
        const data = await fetchThreadTraces(activeThreadId);
        if (!ignore) {
          setTraceRuns(data);
          setTraceDetail(null);
          setActiveTraceId((current) => (data.some((run) => run.trace_id === current) ? current : data[0]?.trace_id || ""));
        }
      } catch (traceError) {
        if (!ignore) {
          setTraceRuns([]);
          setActiveTraceId("");
          setTraceDetail(null);
          toast.error(traceError instanceof Error ? traceError.message : "无法加载 Trace 列表。");
        }
      } finally {
        if (!ignore) {
          setTraceLoading(false);
        }
      }
    }

    loadTraceRuns();

    return () => {
      ignore = true;
    };
  }, [activeThreadId]);

  useEffect(() => {
    if (!activeThreadId || !activeTraceId) {
      setTraceDetail(null);
      return;
    }

    if (activeTraceId === liveTraceId) {
      return;
    }

    let ignore = false;
    setTraceLoading(true);

    async function loadTraceDetail() {
      try {
        const detail = await fetchTraceDetail(activeThreadId, activeTraceId);
        if (!ignore) {
          setTraceDetail(detail);
        }
      } catch (traceError) {
        if (!ignore) {
          setTraceDetail(null);
          toast.error(traceError instanceof Error ? traceError.message : "无法加载 Trace 详情。");
        }
      } finally {
        if (!ignore) {
          setTraceLoading(false);
        }
      }
    }

    loadTraceDetail();

    return () => {
      ignore = true;
    };
  }, [activeThreadId, activeTraceId, liveTraceId]);

  // P6: 历史回放 —— 加载本会话所有 trace 的 detail，供历史 message 派生 stageFlow（D3 顺序对应 via traceId）
  // 用 traceId 列表作依赖键：event_count 高频变化不触发重跑，仅新 trace 加入时重载
  const historyTraceKey = traceRuns.map((r) => r.trace_id).join(",");
  useEffect(() => {
    if (!activeThreadId || !historyTraceKey) {
      setHistoryDetails(new Map());
      return;
    }
    let ignore = false;
    const ids = historyTraceKey.split(",").filter(Boolean);
    async function loadHistoryDetails() {
      const map = new Map<string, TraceDetail>();
      for (const id of ids) {
        try {
          map.set(id, await fetchTraceDetail(activeThreadId, id));
        } catch {
          // 单个 trace 加载失败不阻塞其他
        }
      }
      if (!ignore) setHistoryDetails(map);
    }
    loadHistoryDetails();
    return () => {
      ignore = true;
    };
  }, [activeThreadId, historyTraceKey]);

  const workspacePath = activeWorkspace?.workspace_path ?? activeThread?.workspace_path;
  const currentOutlineMarkdown = result?.thread_id === activeThreadId && result.markdown?.trim() ? result.markdown : outlineMarkdown;

  // 内容面板实时刷新：生成中每 2s 轮询当前面板的 REST 接口（替代原 SSE/EventSource，
  // 因 GET EventSource 在 Next.js dev rewrites 下不可靠，而 REST 走同代理已验证可用）。
  usePanelPolling({
    activeWorkspaceId,
    activePanel,
    loading,
    bootstrapping,
    setters: {
      setNovelChapters,
      setActiveNovelFilename,
      setNovelLoading,
      setStorylineMarkdown,
      setStorylineEntries,
      setActiveStorylineFilename,
      setDetailOutlineChapters,
      setActiveDetailChapterFilename,
      setDetailOutlineLoading,
      setCharacters,
      setActiveCharacterFilename,
      setCharactersLoading,
      setWorldviewMarkdown,
      setWorldviewLoading,
      setOutlineMarkdown,
      setOutlineLoading,
    },
  });

  async function handleCreateWorkspace() {
    const title = newWorkspaceName.trim();
    if (!title || creatingWorkspace) return;

    setCreatingWorkspace(true);

    try {
      const workspace = await createWorkspaceRequest(title, newWorkspaceDomain);

      // 新工作区一定是空的，直接设空值避免触发无意义的 bootstrap 请求
      setThreads([]);
      setActiveThreadId("");
      setResult(null);
      setOutlineMarkdown("");
      setStorylineMarkdown("");
      setStorylineEntries([]);
      setActiveStorylineFilename("");
      setWorldviewMarkdown("");
      setDetailOutlineChapters([]);
      setActiveDetailChapterFilename("");
      setCharacters([]);
      setActiveCharacterFilename("");
      setNovelChapters([]);
      setActiveNovelFilename("");
      setMessages([initialAssistantMessage]);
      skipNextBootstrap.current = true;

      setWorkspaces((current) => [workspace, ...current]);
      setActiveWorkspaceId(workspace.workspace_id);
      setNewWorkspaceName("失忆编剧大纲");
      setNewWorkspaceDomain("writing");
      setWorkspaceCreateOpen(false);
    } catch (createError) {
      toast.error(createError instanceof Error ? createError.message : "无法创建工作目录。");
    } finally {
      setCreatingWorkspace(false);
    }
  }

  async function handleDeleteWorkspace() {
    if (!pendingDeleteWorkspaceId || deletingWorkspace) return;

    setDeletingWorkspace(true);

    try {
      await deleteWorkspaceRequest(pendingDeleteWorkspaceId);
      threadMessagesRef.current.clear();
      setWorkspaces((current) => {
        const next = current.filter((workspace) => workspace.workspace_id !== pendingDeleteWorkspaceId);
        // 如果删的是当前激活的工作目录，切换到剩余的第一个
        if (pendingDeleteWorkspaceId === activeWorkspaceId) {
          setActiveWorkspaceId(next[0]?.workspace_id || "");
        }
        return next;
      });
      // 如果删的是当前激活的工作目录，清空所有面板数据
      if (pendingDeleteWorkspaceId === activeWorkspaceId) {
        setThreads([]);
        setActiveThreadId("");
        setTraceRuns([]);
        setActiveTraceId("");
        setLiveTraceId("");
        setTraceDetail(null);
        setOutlineMarkdown("");
        setStorylineMarkdown("");
        setStorylineEntries([]);
        setActiveStorylineFilename("");
        setWorldviewMarkdown("");
        setDetailOutlineChapters([]);
        setActiveDetailChapterFilename("");
        setNovelChapters([]);
        setActiveNovelFilename("");
        setCharacters([]);
        setActiveCharacterFilename("");
        setResult(null);
        setMessages([initialAssistantMessage]);
        setSessionMenuOpen(false);
      }
      setWorkspaceDeleteOpen(false);
      setPendingDeleteWorkspaceId("");
    } catch (deleteError) {
      toast.error(deleteError instanceof Error ? deleteError.message : "无法删除工作目录。");
    } finally {
      setDeletingWorkspace(false);
    }
  }

  async function handleCreateStyle(name: string, metaStyle: string, storybuildingStyle: string, detailOutlineStyle: string, writingStyle: string) {
    setCreatingStyle(true);
    try {
      const style = await createStyleRequest(name, metaStyle, storybuildingStyle, detailOutlineStyle, writingStyle);
      setStyles((current) => [...current, style]);
    } catch (createStyleError) {
      toast.error(createStyleError instanceof Error ? createStyleError.message : "无法创建风格。");
    } finally {
      setCreatingStyle(false);
    }
  }

  async function handleUpdateStyle(styleId: string, fields: Record<string, string>): Promise<boolean> {
    try {
      const updated = await updateStyleRequest(styleId, fields);
      setStyles((current) => current.map((s) => (s.style_id === updated.style_id ? updated : s)));
      return true;
    } catch (updateError) {
      toast.error(updateError instanceof Error ? updateError.message : "无法更新风格。");
      return false;
    }
  }

  async function handleOptimizeStyle(styleType: string, content: string): Promise<string> {
    try {
      const result = await optimizeStyleRequest(styleType, content);
      return result.optimized;
    } catch (optimizeError) {
      toast.error(optimizeError instanceof Error ? optimizeError.message : "AI 优化失败。");
      return content;
    }
  }

  async function handleDeleteStyle(styleId: string) {
    try {
      await deleteStyleRequest(styleId);
      setStyles((current) => current.filter((s) => s.style_id !== styleId));
    } catch (deleteStyleError) {
      toast.error(deleteStyleError instanceof Error ? deleteStyleError.message : "无法删除风格。");
    }
  }

  async function handleSelectStyle(styleId: string | null) {
    if (!activeWorkspaceId) return;
    try {
      const updated = await activateStyleRequest(activeWorkspaceId, styleId);
      setWorkspaces((current) =>
        current.map((w) => (w.workspace_id === updated.workspace_id ? updated : w)),
      );
    } catch (activateError) {
      toast.error(activateError instanceof Error ? activateError.message : "无法设置风格。");
    }
  }

  async function handleCreateThread() {
    if (!activeWorkspaceId || creatingThread) return;

    if (activeThreadId) {
      threadMessagesRef.current.set(activeThreadId, messagesRef.current);
    }

    setCreatingThread(true);

    try {
      const thread = await createThreadRequest(activeWorkspaceId);
      setThreads((current) => [thread, ...current.filter((item) => item.thread_id !== thread.thread_id)]);
      setMessages([initialAssistantMessage]);
      setResult(null);
      setActiveThreadId(thread.thread_id);
    } catch (createError) {
      toast.error(createError instanceof Error ? createError.message : "无法创建新会话。");
    } finally {
      setCreatingThread(false);
    }
  }

  function handleSelectThread(threadId: string) {
    if (threadId === activeThreadId) return;
    if (activeThreadId) {
      threadMessagesRef.current.set(activeThreadId, messagesRef.current);
    }
    setActiveThreadId(threadId);
  }

  async function handleDeleteThread(threadId: string) {
    if (!threadId || deleting) return;

    setDeleting(true);

    try {
      await deleteThreadRequest(threadId);
      threadMessagesRef.current.delete(threadId);
      setThreads((current) => {
        const next = current.filter((thread) => thread.thread_id !== threadId);
        if (activeThreadId === threadId) {
          setTraceRuns([]);
          setActiveTraceId("");
          setLiveTraceId("");
          setTraceDetail(null);
          setActiveThreadId(next[0]?.thread_id || "");
        }
        return next;
      });
    } catch (deleteError) {
      toast.error(deleteError instanceof Error ? deleteError.message : "删除失败。");
    } finally {
      setDeleting(false);
    }
  }

  async function handleDeleteTrace(traceId: string) {
    if (!activeThreadId || !traceId || deletingTraceId) return;
    const run = traceRuns.find((item) => item.trace_id === traceId);
    if (run?.status === "running") {
      toast.error("运行中的 Trace 不能删除。");
      return;
    }

    setDeletingTraceId(traceId);

    try {
      await deleteTraceRequest(activeThreadId, traceId);
      setTraceRuns((current) => {
        const next = current.filter((item) => item.trace_id !== traceId);
        if (activeTraceId === traceId) {
          setTraceDetail(null);
          setActiveTraceId(next[0]?.trace_id || "");
        }
        if (liveTraceId === traceId) {
          setLiveTraceId("");
        }
        return next;
      });
    } catch (deleteError) {
      const message = deleteError instanceof Error ? deleteError.message : "";
      toast.error(message.includes("409") ? "运行中的 Trace 不能删除。" : "无法删除 Trace。");
    } finally {
      setDeletingTraceId("");
    }
  }

  function handleStopGeneration() {
    // D6 停止信号：先发显式 POST /stop（fire-and-forget），再 abort SSE。
    // 后端据 _user_stop_requested 标记区分"用户主动停止"vs"连接断开"，
    // 两者都收尾成 cancelled，仅 error 文案来源不同。
    if (activeThreadId && liveTraceId) {
      apiFetch(`${API_BASE_URL}/api/screenplay/stop`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ thread_id: activeThreadId, trace_id: liveTraceId }),
        credentials: "include",
      }).catch(() => {
        /* fire-and-forget：失败不影响 abort（连接断会走 client_disconnect 收尾）*/
      });
    }
    abortControllerRef.current?.abort();
  }

  // 决策9：重试 = 重发最后一条 user prompt（失败/停止后一键恢复）
  function handleRetry() {
    // 数据闭环 E3：重试 = 负信号（用户对上次结果不满意），埋点后重发。
    if (liveTraceId) {
      trackRegenerate(liveTraceId);
    }
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (lastUser?.content) {
      void performSubmit(lastUser.content);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (aiDisabled) {
      toast.error("请先在设置页填写你的 API Key，才能使用 AI 生成。");
      return;
    }
    // performSubmit 在 resume 失败时会 re-throw（供 InterviewOptions 解锁）；
    // 表单路径无 InterviewOptions，吞掉避免 unhandled rejection（UI 已在内部处理）。
    try {
      if (activeWorkspace?.domain === "image") {
        await performImageStream({ prompt });
        setPrompt("");
        return;
      }
      await performSubmit(prompt);
    } catch {
      /* performSubmit 内部已处理 UI + toast */
    }
  }

  async function performSubmit(promptText: string) {
    const trimmedPrompt = promptText.trim();
    if (!trimmedPrompt || loading) return;
    // HITL: 若上一条 assistant 处于 awaitingInput，本次提交是对 interrupt 的回答（resume）
    const lastAssistant = [...messagesRef.current].reverse().find((m) => m.role === "assistant");
    const isResume = !!lastAssistant?.awaitingInput;
    // 点3：resume 时复用上一条 assistant 的 trace，把多次 HITL 缝合成同一条 trace
    const resumeTraceId = isResume ? lastAssistant?.traceId ?? null : null;
    // D4：resume 时立即清空上一条 awaitingInput，让旧 HITL 选项框卸载（根治状态残留 + 防 isResume 误命中/答案串台）。
    // 在追加新消息之前、isResume 判定之后执行：index 稳定，且不影响本次 resume 判定。
    // 配合 D3（仅最后一条 assistant 渲染选项框）：提交后旧 assistant 退居非末位，D3 让选项框即时卸载，
    // 此处同步清空 awaitingInput 字段，保证 isResume / awaitingWithOptions 判定干净。
    const prevAwaitingInputIdx = isResume
      ? messagesRef.current.lastIndexOf(lastAssistant!)
      : -1;
    if (prevAwaitingInputIdx >= 0) {
      setMessages((current) => {
        const target = current[prevAwaitingInputIdx];
        if (!target || target.role !== "assistant" || !target.awaitingInput) return current;
        const next = [...current];
        // 清空 awaitingInput（选项框消失、isResume 不再误命中），保留 content（问题文本留作历史）。
        next[prevAwaitingInputIdx] = { ...target, awaitingInput: undefined };
        return next;
      });
    }
    if (!activeWorkspaceId) {
      toast.error("请先选择或创建一个工作目录。");
      return;
    }

    setLoading(true);
    setResult(null);
    if (resumeTraceId) {
      // resume：激活 trace-1 的 live 追踪；保留其完整 detail（命中即用，否则用 traceRuns 骨架兜底）。
      // 不清空——后续 trace_event（trace_id===trace-1）才能经 appendLiveTraceEvent 缝合进完整历史。
      setLiveTraceId(resumeTraceId);
      setTraceDetail((current) => {
        if (current?.run.trace_id === resumeTraceId) return current;
        const run = traceRuns.find((r) => r.trace_id === resumeTraceId);
        return run ? { run, events: [], nodes: [], context: [], todos: [] } : current;
      });
    } else {
      // 新 trace：清空，等待 run_start 重建
      setLiveTraceId("");
      setTraceDetail(null);
    }

    if (activeThreadId) {
      threadMessagesRef.current.set(activeThreadId, messagesRef.current);
    }

    const assistantIdx = messages.length + 1;
    setMessages((current) => [
      ...current,
      { role: "user", content: trimmedPrompt },
      {
        role: "assistant",
        content: "正在执行...",
        contentFormat: "markdown",
        tools: [],
        traceId: resumeTraceId ?? undefined, // resume 时继承 trace-1（复用，不再依赖后端 run_start）
      },
    ]);
    setPrompt("");

    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    try {
      let userMessageThreadId = activeThreadId;
      let shouldNameThread = activeThread?.session_name.startsWith("会话 ") ?? false;

      if (!userMessageThreadId) {
        const thread = await createThreadRequest(activeWorkspaceId, getSessionTitle(trimmedPrompt));
        userMessageThreadId = thread.thread_id;
        shouldNameThread = false;
        threadMessagesRef.current.set(thread.thread_id, messagesRef.current);
        setThreads((current) => [thread, ...current.filter((item) => item.thread_id !== thread.thread_id)]);
        setActiveThreadId(thread.thread_id);
      }

      const nextSessionName = shouldNameThread ? getSessionTitle(trimmedPrompt) : "";
      if (nextSessionName) {
        updateThreadRequest(userMessageThreadId, nextSessionName)
          .then((thread) => {
            setThreads((current) =>
              current.map((item) => (item.thread_id === thread.thread_id ? thread : item)),
            );
          })
          .catch((renameError) => {
            toast.error(renameError instanceof Error ? renameError.message : "无法更新会话名称。");
          });
      }

      const response = await fetch(`${API_BASE_URL}/api/screenplay/generate/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          isResume
            ? { thread_id: userMessageThreadId, resume: trimmedPrompt, trace_id: resumeTraceId ?? undefined }
            : { thread_id: userMessageThreadId, prompt: trimmedPrompt },
        ),
        credentials: "include",
        signal: abortController.signal,
      });

      if (!response.ok) {
        throw new Error(`API returned ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error("Response body is not readable");

      const decoder = new TextDecoder();
      let buffer = "";
      let streamedText = "";
      let hasModelOutput = false;
      let finalData: ScreenplayResponse | null = null;

      while (true) {
        // 心跳超时检测：让 read() 与定时器竞争。连接被静默掐断时 TCP 处于半关闭，
        // read() 会无限阻塞，必须靠超时主动挣脱，否则前端永远卡在“运行中”。
        let heartbeatTimer: ReturnType<typeof setTimeout> | undefined;
        const heartbeatTimeout = new Promise<never>((_, reject) => {
          heartbeatTimer = setTimeout(() => reject(new Error("HEARTBEAT_TIMEOUT")), HEARTBEAT_TIMEOUT_MS);
        });

        let done = false;
        let value: Uint8Array | undefined;
        try {
          const chunk = (await Promise.race([
            reader.read(),
            heartbeatTimeout,
          ])) as ReadableStreamReadResult<Uint8Array>;
          done = chunk.done;
          value = chunk.value;
        } catch {
          // 超时触发：read() 仍在 pending，主动取消以释放底层连接，再上抛由 catch 兜底。
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
              const event = {
                type: eventType as StreamEvent["type"],
                data: JSON.parse(eventData) as Record<string, unknown>,
              };

              if (event.type === "model_stream") {
                streamedText += String(event.data.content ?? "");
              } else if (event.type === "trace_event") {
                const traceEvent = event.data as TraceLogEvent;
                // D2: run_start 时把 trace_id 绑定到本次提交的 assistant message
                if (traceEvent.type === "run_start") {
                  setMessages((current) =>
                    updateAssistantMessage(current, assistantIdx, (message) => ({ ...message, traceId: traceEvent.trace_id })),
                  );
                }
                setTraceRuns((current) => {
                  if (traceEvent.type === "run_start") {
                    return upsertTraceRun(current, runFromTraceEvent(traceEvent));
                  }
                  return current.map((run) => (run.trace_id === traceEvent.trace_id ? updateTraceRunFromEvent(run, traceEvent) : run));
                });
                setTraceDetail((current) => {
                  const fallbackRun = current?.run.trace_id === traceEvent.trace_id
                    ? current.run
                    : traceRuns.find((run) => run.trace_id === traceEvent.trace_id) ?? fallbackRunFromTraceEvent(traceEvent, activeThread, activeWorkspaceId);
                  return appendLiveTraceEvent(current, traceEvent, fallbackRun);
                });
                setActiveTraceId(traceEvent.trace_id);
                setLiveTraceId(traceEvent.trace_id);
                if (traceEvent.type === "run_end" || traceEvent.type === "run_error") {
                  setLiveTraceId("");
                }
              } else if (event.type === "trace_snapshot") {
                const detail = event.data as TraceDetail;
                setTraceDetail(detail);
                setTraceRuns((current) => upsertTraceRun(current, detail.run));
                setActiveTraceId(detail.run.trace_id);
                setLiveTraceId(detail.run.status === "running" ? detail.run.trace_id : "");
              } else if (event.type === "model_output") {
                hasModelOutput = true;
              } else if (event.type === "tool_call") {
                setMessages((current) =>
                  updateAssistantMessage(current, assistantIdx, (message) => ({
                    ...message,
                    tools: upsertRunningTool(message.tools, event),
                  })),
                );
              } else if (event.type === "tool_output") {
                setMessages((current) =>
                  updateAssistantMessage(current, assistantIdx, (message) => ({
                    ...message,
                    tools: markToolComplete(message.tools, event),
                  })),
                );
              } else if (event.type === "tool_error") {
                setMessages((current) =>
                  updateAssistantMessage(current, assistantIdx, (message) => ({
                    ...message,
                    tools: markToolFailed(message.tools, event),
                  })),
                );
              } else if (event.type === "final") {
                finalData = event.data as ScreenplayResponse;
              } else if (event.type === "interrupt") {
                const iv = event.data as {
                  kind?: string;
                  question?: string;
                  options?: AskUserOption[] | null;
                  multi_select?: boolean;
                  source?: string;
                  // image_review（DD4）
                  round?: number;
                  versions?: unknown[];
                };
                const interruptKind = iv.kind ?? "choice";
                setMessages((current) =>
                  updateAssistantMessage(current, assistantIdx, (message) => ({
                    ...message,
                    content:
                      interruptKind === "image_review"
                        ? `第 ${iv.round ?? "?"} 轮图像评审：3 版 6 图已生成，请打分`
                        : iv.question || "等待你的输入",
                    awaitingInput: {
                      kind: interruptKind,
                      question: iv.question || "",
                      options: iv.options ?? null,
                      multi_select: iv.multi_select ?? false,
                      source: iv.source,
                      // image_review 透传完整 payload（DD4）
                      round: iv.round,
                      versions: iv.versions,
                    },
                  })),
                );
              }
            } catch {
              // SSE streams can include partial model/debug chunks that are not JSON payloads.
            }
            eventType = "";
            eventData = "";
          }
        }

        setMessages((current) =>
          updateAssistantMessage(current, assistantIdx, (message) => {
            if (streamedText) {
              return {
                ...message,
                content: streamedText,
                contentFormat: "markdown",
              };
            }
            // 工具/子代理阶段：过程反馈交给 StageFlowView（焦点文案+阶段进度），content 保持简洁占位
            return message;
          }),
        );
      }

      if (finalData) {
        setResult(finalData);
        setWorkspaces((current) =>
          current.map((workspace) =>
            workspace.workspace_id === finalData!.workspace_id ? { ...workspace, updated_at: new Date().toISOString() } : workspace,
          ),
        );
        setThreads((current) =>
          current.map((thread) =>
            thread.thread_id === finalData!.thread_id ? { ...thread, updated_at: new Date().toISOString() } : thread,
          ),
        );
        setMessages((current) =>
          updateAssistantMessage(current, assistantIdx, (message) => ({
            ...message,
            status: "completed",
            content:
              streamedText ||
              finalData?.markdown?.trim() ||
              `已生成《${finalData.session_name}》的故事材料，工作目录是 ${finalData.workspace_path}`,
            contentFormat: "markdown",
          })),
        );
      }
    } catch (submitError) {
      if (submitError instanceof DOMException && submitError.name === "AbortError") {
        setLiveTraceId("");
        setMessages((current) =>
          updateAssistantMessage(current, assistantIdx, (message) => ({
            ...message,
            status: "stopped",
            content: message.content === "正在执行..." ? "已手动停止。" : `${message.content}\n\n已手动停止。`,
            contentFormat: "markdown",
          })),
        );
        if (isResume) throw submitError; // 点2：resume 失败/中断 → 解锁 InterviewOptions
        return;
      }

      if (submitError instanceof Error && submitError.message === "HEARTBEAT_TIMEOUT") {
        const heartbeatMessage = `连接已断开（超过 ${HEARTBEAT_TIMEOUT_MS / 1000} 秒未收到数据），请重试。`;
        setLiveTraceId("");
        toast.error(heartbeatMessage);
        setMessages((current) =>
          updateAssistantMessage(current, assistantIdx, (message) => ({
            ...message,
            status: "failed",
            content:
              message.content === "正在执行..." ? `⚠️ ${heartbeatMessage}` : `${message.content}\n\n⚠️ ${heartbeatMessage}`,
            contentFormat: "markdown",
          })),
        );
        if (isResume) throw submitError; // 点2：resume 失败 → 解锁
        return;
      }

      toast.error(submitError instanceof Error ? submitError.message : "Unexpected request failure.");
      setMessages((current) =>
        updateAssistantMessage(current, assistantIdx, (message) => ({
          ...message,
          status: "failed",
          content:
            message.content === "正在执行..."
              ? "请求失败了。检查后端是否启动后，可以在同一个会话里重试。"
              : `${message.content}\n\n⚠️ 请求失败，可重试。`,
          contentFormat: "markdown",
        })),
      );
      if (isResume) throw submitError; // 点2：resume 失败 → 解锁
    } finally {
      if (abortControllerRef.current === abortController) {
        abortControllerRef.current = null;
      }
      setLoading(false);
    }
  }

  /**
   * image review 的 resume 处理（DD4 结构化反馈）。
   * 发送结构化 resume 对象到 /api/image/generate/stream，消费 SSE 更新消息。
   * 精简版：处理 interrupt（下一轮评审）+ final（收尾）+ error。
   * 支持两种模式：首次提交（prompt）/ resume（结构化反馈对象）。
   */
  async function performImageStream(
    opts: { prompt: string } | { resume: import("../lib/api").ImageReviewResume },
  ) {
    if (!activeThreadId || loading) return;
    const isResume = "resume" in opts;
    if (isResume) {
      const lastAssistant = [...messagesRef.current].reverse().find((m) => m.role === "assistant");
      if (!lastAssistant?.awaitingInput) return;
      setMessages((current) => {
        const idx = current.findIndex((m) => m === lastAssistant);
        if (idx < 0) return current;
        const next = [...current];
        next[idx] = { ...lastAssistant, awaitingInput: undefined };
        return next;
      });
    }
    setLoading(true);
    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    if (!isResume) {
      // 首次提交：追加 user 消息
      setMessages((current) => [
        ...current,
        { role: "user", content: opts.prompt, contentFormat: "text" },
      ]);
    }
    setMessages((current) => [
      ...current,
      { role: "assistant", content: isResume ? "正在优化..." : "正在生成...", contentFormat: "markdown", tools: [] },
    ]);
    try {
      const body = isResume
        ? { thread_id: activeThreadId, prompt: "", resume: opts.resume }
        : { thread_id: activeThreadId, prompt: opts.prompt };
      const response = await fetch(`${API_BASE_URL}/api/image/generate/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        credentials: "include",
        signal: abortController.signal,
      });
      if (!response.ok || !response.body) throw new Error(`image stream failed: ${response.status}`);
      const reader = response.body.getReader();
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
              setMessages((current) =>
                updateAssistantMessage(current, current.length - 1, (m) => ({
                  ...m, content: streamedText, contentFormat: "markdown",
                })),
              );
            } else if (event.type === "interrupt") {
              const iv = event.data as {
                kind?: string; round?: number; versions?: unknown[];
                question?: string; options?: AskUserOption[] | null; multi_select?: boolean; source?: string;
              };
              const interruptKind = iv.kind ?? "choice";
              setMessages((current) =>
                updateAssistantMessage(current, current.length - 1, (m) => ({
                  ...m,
                  content: interruptKind === "image_review"
                    ? `第 ${iv.round ?? "?"} 轮图像评审：请打分`
                    : iv.question || "等待输入",
                  awaitingInput: {
                    kind: interruptKind, question: iv.question || "",
                    options: iv.options ?? null, multi_select: iv.multi_select ?? false,
                    source: iv.source, round: iv.round, versions: iv.versions,
                  } as typeof m.awaitingInput,
                })),
              );
              return;
            } else if (event.type === "final") {
              const data = event.data as { content?: string };
              setMessages((current) =>
                updateAssistantMessage(current, current.length - 1, (m) => ({
                  ...m, content: data.content ?? "完成", contentFormat: "markdown",
                })),
              );
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
      if (abortControllerRef.current === abortController) abortControllerRef.current = null;
      setLoading(false);
    }
  }

  // 未登录（或在跳转 /login 中）：不渲染主界面，避免闪烁 + 触发 401 雪崩
  if (!authChecked) {
    return null;
  }

  // 未填 API Key：禁用 AI 生成，提交时拦截并引导去设置页
  const aiDisabled = !hasApiKey;

  return (
    <>
      <AppShell
        topBar={
          <TopBar
            workspaces={workspaces}
            activeWorkspaceId={activeWorkspaceId}
            creatingWorkspace={creatingWorkspace}
            deletingWorkspace={deletingWorkspace}
            theme={theme}
            username={authUser?.username ?? ""}
            isAdmin={authUser?.is_admin ?? false}
            hasApiKey={hasApiKey}
            onWorkspaceChange={setActiveWorkspaceId}
            onCreateWorkspace={() => setWorkspaceCreateOpen(true)}
            onDeleteWorkspace={(workspaceId: string) => {
              setPendingDeleteWorkspaceId(workspaceId);
              setWorkspaceDeleteOpen(true);
            }}
            onThemeToggle={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
            onLogout={handleLogout}
          />
        }
        sidebar={<Sidebar activePanel={activePanel} onPanelChange={setActivePanel} />}
      >
        {activePanel === "chat" ? (
          <ChatPanel
            messages={messages}
            prompt={prompt}
            loading={loading}
            threads={threads}
            activeThreadId={activeThreadId}
            hasActiveWorkspace={Boolean(activeWorkspaceId)}
            activeStyleName={activeStyleName}
            sessionMenuOpen={sessionMenuOpen}
            creatingThread={creatingThread}
            deleting={deleting}
            onPromptChange={setPrompt}
            onSubmit={handleSubmit}
            onResumeSubmit={async (resumeText) => {
              if (aiDisabled) {
                toast.error("请先在设置页填写你的 API Key，才能使用 AI 生成。");
                return;
              }
              await performSubmit(resumeText);
            }}
            onImageReviewSubmit={async (resume) => {
              if (aiDisabled) {
                toast.error("请先在设置页填写你的 API Key，才能使用 AI 生成。");
                return;
              }
              // image review 的 resume 走 image generate stream 端点（结构化对象，DD4）
              await performImageStream({ resume });
            }}
            onStop={handleStopGeneration}
            onToggleSessionMenu={() => setSessionMenuOpen((open) => !open)}
            onCloseSessionMenu={() => setSessionMenuOpen(false)}
            onCreateThread={handleCreateThread}
            onSelectThread={handleSelectThread}
            onDeleteThread={handleDeleteThread}
            onOpenStyleModal={() => setStyleModalOpen(true)}
            stageFlows={stageFlows}
            onRetry={handleRetry}
          />
        ) : null}

        {activePanel === "novel" ? (
          <NovelPanel
            chapters={novelChapters}
            activeFilename={activeNovelFilename}
            loading={novelLoading}
            onSelectChapter={setActiveNovelFilename}
            exportUrl={activeWorkspaceId ? workspaceNovelPdfUrl(activeWorkspaceId) : undefined}
            wordExportUrl={activeWorkspaceId ? workspaceNovelWordUrl(activeWorkspaceId) : undefined}
            onCopyContent={(text) => liveTraceId && trackCopy(liveTraceId, text)}
          />
        ) : null}

        {activePanel === "script" ? (
          <ScriptPanel
            storylineMarkdown={storylineMarkdown}
            storylineEntries={storylineEntries}
            activeStorylineFilename={activeStorylineFilename}
            loading={outlineLoading}
            onSelectStoryline={setActiveStorylineFilename}
          />
        ) : null}

        {activePanel === "detail_outline" ? (
          <DetailOutlinePanel
            chapters={detailOutlineChapters}
            activeFilename={activeDetailChapterFilename}
            loading={detailOutlineLoading}
            onSelectChapter={setActiveDetailChapterFilename}
          />
        ) : null}

        {activePanel === "characters" ? (
          <CharactersPanel
            characters={characters}
            activeFilename={activeCharacterFilename}
            loading={charactersLoading}
            onSelectCharacter={setActiveCharacterFilename}
          />
        ) : null}

        {activePanel === "worldview" ? (
          <WorldviewPanel
            workspacePath={workspacePath}
            markdown={worldviewMarkdown}
            loading={worldviewLoading}
          />
        ) : null}

        {activePanel === "storyline" ? <StorylinePanel workspaceId={activeWorkspaceId} /> : null}

        {activePanel === "trace" ? (
          <TracePanel
            runs={traceRuns}
            detail={traceDetail}
            activeTraceId={activeTraceId}
            loading={traceLoading}
            hasActiveThread={Boolean(activeThreadId)}
            deletingTraceId={deletingTraceId}
            onSelectTrace={setActiveTraceId}
            onDeleteTrace={handleDeleteTrace}
          />
        ) : null}

      </AppShell>

      {workspaceCreateOpen ? (
        <div className="modal-overlay" role="presentation">
          <section className="modal-content" role="dialog" aria-modal="true" aria-labelledby="workspace-create-title">
            <h2 className="modal-title" id="workspace-create-title">
              新建工作目录
            </h2>
            <p className="modal-description">选择类型并输入名称，创建后会直接切换。</p>
            <form
              className="workspace-create-form"
              onSubmit={(event) => {
                event.preventDefault();
                handleCreateWorkspace();
              }}
            >
              <div className="workspace-domain-select">
                <label className={`domain-option${newWorkspaceDomain === "writing" ? " selected" : ""}`}>
                  <input
                    type="radio"
                    name="workspace-domain"
                    value="writing"
                    checked={newWorkspaceDomain === "writing"}
                    onChange={() => setNewWorkspaceDomain("writing")}
                    disabled={creatingWorkspace}
                  />
                  <span className="domain-option-body">
                    <strong>✍️ 写作</strong>
                    <small>小说/剧本创作</small>
                  </span>
                </label>
                <label className={`domain-option${newWorkspaceDomain === "image" ? " selected" : ""}`}>
                  <input
                    type="radio"
                    name="workspace-domain"
                    value="image"
                    checked={newWorkspaceDomain === "image"}
                    onChange={() => setNewWorkspaceDomain("image")}
                    disabled={creatingWorkspace}
                  />
                  <span className="domain-option-body">
                    <strong>🎨 文生图</strong>
                    <small>图片生成与优化</small>
                  </span>
                </label>
              </div>
              <input
                className="thread-input workspace-create-input"
                value={newWorkspaceName}
                onChange={(event) => setNewWorkspaceName(event.target.value)}
                placeholder={newWorkspaceDomain === "image" ? "请输入图片主题名" : "请输入新剧本名"}
                autoFocus
                disabled={creatingWorkspace}
              />
              <div className="modal-actions">
                <button
                  className="modal-button modal-cancel"
                  type="button"
                  onClick={() => setWorkspaceCreateOpen(false)}
                  disabled={creatingWorkspace}
                >
                  取消
                </button>
                <button className="modal-button modal-primary" type="submit" disabled={creatingWorkspace || !newWorkspaceName.trim()}>
                  {creatingWorkspace ? "创建中" : "创建"}
                </button>
              </div>
            </form>
          </section>
        </div>
      ) : null}

      <ConfirmDialog
        open={workspaceDeleteOpen}
        title={`删除工作目录「${workspaces.find((w) => w.workspace_id === pendingDeleteWorkspaceId)?.title || ""}」？`}
        description="这会删除该工作目录以及目录下的所有创作会话。该操作不可撤销。"
        confirmLabel="删除工作目录"
        loading={deletingWorkspace}
        onConfirm={handleDeleteWorkspace}
        onCancel={() => {
          setWorkspaceDeleteOpen(false);
          setPendingDeleteWorkspaceId("");
        }}
      />

      {styleModalOpen ? (
        <StyleModal
          styles={styles}
          activeStyleId={activeWorkspace?.active_style_id ?? null}
          creating={creatingStyle}
          onCreateStyle={handleCreateStyle}
          onUpdateStyle={handleUpdateStyle}
          onDeleteStyle={handleDeleteStyle}
          onSelectStyle={handleSelectStyle}
          onOptimizeStyle={handleOptimizeStyle}
          onClose={() => setStyleModalOpen(false)}
        />
      ) : null}
    </>
  );
}
