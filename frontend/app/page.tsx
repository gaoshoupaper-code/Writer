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
import { Sidebar } from "../components/workspace/Sidebar";
import { StyleModal } from "../components/workspace/StyleModal";
import { TopBar } from "../components/workspace/TopBar";
import { TracePanel } from "../components/workspace/TracePanel";
import { WorldviewPanel } from "../components/workspace/WorldviewPanel";
import {
  API_BASE_URL,
  activateStyle as activateStyleRequest,
  createStyle as createStyleRequest,
  createThread as createThreadRequest,
  createWorkspace as createWorkspaceRequest,
  deleteStyle as deleteStyleRequest,
  deleteThread as deleteThreadRequest,
  deleteTrace as deleteTraceRequest,
  deleteWorkspace as deleteWorkspaceRequest,
  fetchInit,
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
  optimizeStyle as optimizeStyleRequest,
  updateStyle as updateStyleRequest,
  updateThread as updateThreadRequest,
  workspaceNovelPdfUrl,
  workspaceNovelWordUrl,
} from "../lib/api";
import { appendLiveTraceEvent } from "../lib/trace";
import type {
  CharacterMarkdownFile,
  ChatMessage,
  DetailOutlineChapter,
  ScreenplayResponse,
  Style,
  StreamEvent,
  ThreadSummary,
  ToolStatus,
  TraceDetail,
  TraceLogEvent,
  TraceRunSummary,
  StorylineEntry,
  VolumeChapter,
  WorkspaceCharacterContent,
  WorkspaceDetailOutlineContent,
  WorkspaceNovelContent,
  WorkspaceOutlineContent,
  WorkspacePanel,
  WorkspaceStorylineContent,
  WorkspaceVolumeContent,
  WorkspaceWorldviewContent,
  WorkspaceSummary,
} from "../lib/types";

type ThemeMode = "light" | "dark";

const initialPrompt = "";
const themeStorageKey = "writer-theme";
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

function upsertRunningTool(tools: ToolStatus[] | undefined, event: StreamEvent) {
  const toolName = getToolName(event);
  const callId = getToolCallId(event);
  const parentKey = getToolParentKey(event);
  const subagentName = getSubagentName(event);
  const nextTools = [...(tools ?? [])];
  const lookupKey = callId;

  if (lookupKey) {
    const existingIndex = nextTools.findIndex((tool) => tool.key === lookupKey);
    if (existingIndex >= 0) {
      nextTools[existingIndex] = {
        ...nextTools[existingIndex],
        name: toolName,
        status: "running",
      };
      return nextTools;
    }

    nextTools.push({
      key: lookupKey,
      name: toolName,
      status: "running",
      parentKey,
      subagentName,
    });
    return nextTools;
  }

  nextTools.push({
    key: buildToolKey(toolName, "", nextTools.length),
    name: toolName,
    status: "running",
    parentKey,
    subagentName,
  });
  return nextTools;
}

function markToolComplete(tools: ToolStatus[] | undefined, event: StreamEvent) {
  const toolName = getToolName(event);
  const eventCallId = getToolCallId(event);
  const nextTools = [...(tools ?? [])];

  if (eventCallId) {
    for (let index = 0; index < nextTools.length; index += 1) {
      if (nextTools[index].key === eventCallId && nextTools[index].status === "running") {
        nextTools[index] = { ...nextTools[index], status: "done" };
        return nextTools;
      }
    }
  }

  for (let index = nextTools.length - 1; index >= 0; index -= 1) {
    if (nextTools[index].name === toolName && nextTools[index].status === "running") {
      nextTools[index] = { ...nextTools[index], status: "done" };
      return nextTools;
    }
  }

  for (let index = nextTools.length - 1; index >= 0; index -= 1) {
    if (nextTools[index].name === toolName) {
      nextTools[index] = { ...nextTools[index], status: "done" };
      return nextTools;
    }
  }

  nextTools.push({
    key: buildToolKey(toolName, "", nextTools.length),
    name: toolName,
    status: "done",
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
  const [novelMarkdown, setNovelMarkdown] = useState("");
  const [novelSource, setNovelSource] = useState("");
  const [novelChapterCount, setNovelChapterCount] = useState(0);
  const [novelLoading, setNovelLoading] = useState(false);
  const [characters, setCharacters] = useState<CharacterMarkdownFile[]>([]);
  const [charactersLoading, setCharactersLoading] = useState(false);
  const [activeCharacterFilename, setActiveCharacterFilename] = useState("");
  const [worldviewMarkdown, setWorldviewMarkdown] = useState("");
  const [worldviewLoading, setWorldviewLoading] = useState(false);
  const [volumeChapters, setVolumeChapters] = useState<VolumeChapter[]>([]);
  const [activeVolumeFilename, setActiveVolumeFilename] = useState("");
  const [storylineMarkdown, setStorylineMarkdown] = useState("");
  const [storylineEntries, setStorylineEntries] = useState<StorylineEntry[]>([]);
  const [activeStorylineFilename, setActiveStorylineFilename] = useState("");
  const [outlineTab, setOutlineTab] = useState<"outline" | "storyline" | "volume">("outline");
  const [traceRuns, setTraceRuns] = useState<TraceRunSummary[]>([]);
  const [activeTraceId, setActiveTraceId] = useState("");
  const [liveTraceId, setLiveTraceId] = useState("");
  const [traceDetail, setTraceDetail] = useState<TraceDetail | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [creatingWorkspace, setCreatingWorkspace] = useState(false);
  const [creatingThread, setCreatingThread] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deletingTraceId, setDeletingTraceId] = useState("");
  const [deletingWorkspace, setDeletingWorkspace] = useState(false);
  const [theme, setTheme] = useState<ThemeMode>("light");
  const [themeReady, setThemeReady] = useState(false);
  const [workspaceCreateOpen, setWorkspaceCreateOpen] = useState(false);
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

  // ── 首次加载：2 个请求完成全部初始化 ──
  const bootstrappedRef = useRef(false);

  useEffect(() => {
    if (bootstrappedRef.current) return;
    bootstrappedRef.current = true;

    let ignore = false;

    async function bootstrap() {
      try {
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
        if (data.volume) {
          setVolumeChapters(data.volume.chapters);
          setActiveVolumeFilename(
            data.volume.chapters[0]?.filename || "",
          );
        }
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
          setNovelMarkdown(data.novel.markdown);
          setNovelSource(data.novel.source);
          setNovelChapterCount(data.novel.chapter_count);
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
        if (bootData.volume) {
          setVolumeChapters(bootData.volume.chapters);
          setActiveVolumeFilename(
            bootData.volume.chapters.some((c) => c.filename === activeVolumeFilename)
              ? activeVolumeFilename
              : bootData.volume.chapters[0]?.filename || "",
          );
        } else {
          setVolumeChapters([]);
          setActiveVolumeFilename("");
        }
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
          setNovelMarkdown(bootData.novel.markdown);
          setNovelSource(bootData.novel.source);
          setNovelChapterCount(bootData.novel.chapter_count);
        } else {
          setNovelMarkdown("");
          setNovelSource("");
          setNovelChapterCount(0);
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

  const workspacePath = activeWorkspace?.workspace_path ?? activeThread?.workspace_path;
  const currentOutlineMarkdown = result?.thread_id === activeThreadId && result.markdown?.trim() ? result.markdown : outlineMarkdown;

  // Workspace file watcher: real-time panel updates via SSE
  useEffect(() => {
    if (!activeWorkspaceId) return;

    const es = new EventSource(`${API_BASE_URL}/api/workspaces/${activeWorkspaceId}/watch`);

    es.addEventListener("outline", (e) => {
      try {
        const d = JSON.parse(e.data) as WorkspaceOutlineContent;
        setOutlineMarkdown(d.markdown);
        setOutlineLoading(false);
      } catch {}
    });

    es.addEventListener("storyline", (e) => {
      try {
        const d = JSON.parse(e.data) as WorkspaceStorylineContent;
        setStorylineMarkdown(d.index_markdown);
        setStorylineEntries(d.entries);
        setActiveStorylineFilename((cur) =>
          d.entries.some((entry) => entry.filename === cur) ? cur : d.entries[0]?.filename || "",
        );
        setOutlineLoading(false);
      } catch {}
    });

    es.addEventListener("worldview", (e) => {
      try {
        const d = JSON.parse(e.data) as WorkspaceWorldviewContent;
        setWorldviewMarkdown(d.markdown);
        setWorldviewLoading(false);
      } catch {}
    });

    es.addEventListener("volume", (e) => {
      try {
        const d = JSON.parse(e.data) as WorkspaceVolumeContent;
        setVolumeChapters(d.chapters);
        setActiveVolumeFilename((cur) =>
          d.chapters.some((c) => c.filename === cur) ? cur : d.chapters[0]?.filename || "",
        );
      } catch {}
    });

    es.addEventListener("detail_outline", (e) => {
      try {
        const d = JSON.parse(e.data) as WorkspaceDetailOutlineContent;
        setDetailOutlineChapters(d.chapters);
        setActiveDetailChapterFilename((cur) =>
          d.chapters.some((c) => c.filename === cur) ? cur : d.chapters[0]?.filename || "",
        );
        setDetailOutlineLoading(false);
      } catch {}
    });

    es.addEventListener("characters", (e) => {
      try {
        const d = JSON.parse(e.data) as WorkspaceCharacterContent;
        setCharacters(d.characters);
        setActiveCharacterFilename((cur) =>
          d.characters.some((c) => c.filename === cur) ? cur : d.characters[0]?.filename || "",
        );
        setCharactersLoading(false);
      } catch {}
    });

    es.addEventListener("novel", (e) => {
      try {
        const d = JSON.parse(e.data) as WorkspaceNovelContent;
        setNovelMarkdown(d.markdown);
        setNovelSource(d.source);
        setNovelChapterCount(d.chapter_count);
        setNovelLoading(false);
      } catch {}
    });

    return () => {
      es.close();
    };
  }, [activeWorkspaceId]);

  async function handleCreateWorkspace() {
    const outlineName = newWorkspaceName.trim();
    if (!outlineName || creatingWorkspace) return;

    setCreatingWorkspace(true);

    try {
      const workspace = await createWorkspaceRequest(outlineName);

      // 新工作区一定是空的，直接设空值避免触发无意义的 bootstrap 请求
      setThreads([]);
      setActiveThreadId("");
      setResult(null);
      setOutlineMarkdown("");
      setStorylineMarkdown("");
      setStorylineEntries([]);
      setActiveStorylineFilename("");
      setWorldviewMarkdown("");
      setVolumeChapters([]);
      setActiveVolumeFilename("");
      setDetailOutlineChapters([]);
      setActiveDetailChapterFilename("");
      setCharacters([]);
      setActiveCharacterFilename("");
      setNovelMarkdown("");
      setNovelSource("");
      setNovelChapterCount(0);
      setMessages([initialAssistantMessage]);
      skipNextBootstrap.current = true;

      setWorkspaces((current) => [workspace, ...current]);
      setActiveWorkspaceId(workspace.workspace_id);
      setNewWorkspaceName("失忆编剧大纲");
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
        setVolumeChapters([]);
        setActiveVolumeFilename("");
        setDetailOutlineChapters([]);
        setActiveDetailChapterFilename("");
        setNovelMarkdown("");
        setNovelSource("");
        setNovelChapterCount(0);
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
    abortControllerRef.current?.abort();
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const trimmedPrompt = prompt.trim();
    if (!trimmedPrompt || loading) return;
    if (!activeWorkspaceId) {
      toast.error("请先选择或创建一个工作目录。");
      return;
    }

    setLoading(true);
    setResult(null);
    setLiveTraceId("");
    setTraceDetail(null);

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
        body: JSON.stringify({
          thread_id: userMessageThreadId,
          prompt: trimmedPrompt,
        }),
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
        const { done, value } = await reader.read();
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
              } else if (event.type === "final") {
                finalData = event.data as ScreenplayResponse;
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

            if (hasModelOutput) {
              return {
                ...message,
                content: "模型正在思考并调用工具...",
                contentFormat: "markdown",
              };
            }

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
            content: message.content === "正在执行..." ? "已手动停止。" : `${message.content}\n\n已手动停止。`,
            contentFormat: "markdown",
          })),
        );
        return;
      }

      toast.error(submitError instanceof Error ? submitError.message : "Unexpected request failure.");
      setMessages((current) => [
        ...current,
        {
          role: "assistant",
          content: "请求失败了。检查后端是否启动后，可以在同一个会话里重试。",
        },
      ]);
    } finally {
      if (abortControllerRef.current === abortController) {
        abortControllerRef.current = null;
      }
      setLoading(false);
    }
  }

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
            onWorkspaceChange={setActiveWorkspaceId}
            onCreateWorkspace={() => setWorkspaceCreateOpen(true)}
            onDeleteWorkspace={(workspaceId: string) => {
              setPendingDeleteWorkspaceId(workspaceId);
              setWorkspaceDeleteOpen(true);
            }}
            onThemeToggle={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
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
            onStop={handleStopGeneration}
            onToggleSessionMenu={() => setSessionMenuOpen((open) => !open)}
            onCloseSessionMenu={() => setSessionMenuOpen(false)}
            onCreateThread={handleCreateThread}
            onSelectThread={handleSelectThread}
            onDeleteThread={handleDeleteThread}
            onOpenStyleModal={() => setStyleModalOpen(true)}
          />
        ) : null}

        {activePanel === "novel" ? (
          <NovelPanel
            workspacePath={workspacePath}
            markdown={novelMarkdown}
            loading={novelLoading}
            source={novelSource}
            chapterCount={novelChapterCount}
            exportUrl={activeWorkspaceId ? workspaceNovelPdfUrl(activeWorkspaceId) : undefined}
            wordExportUrl={activeWorkspaceId ? workspaceNovelWordUrl(activeWorkspaceId) : undefined}
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
              新建剧本
            </h2>
            <p className="modal-description">输入新剧本名，创建后会直接切换到这个剧本。</p>
            <form
              className="workspace-create-form"
              onSubmit={(event) => {
                event.preventDefault();
                handleCreateWorkspace();
              }}
            >
              <input
                className="thread-input workspace-create-input"
                value={newWorkspaceName}
                onChange={(event) => setNewWorkspaceName(event.target.value)}
                placeholder="请输入新剧本名"
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
        title={`删除工作目录「${workspaces.find((w) => w.workspace_id === pendingDeleteWorkspaceId)?.outline_name || ""}」？`}
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
