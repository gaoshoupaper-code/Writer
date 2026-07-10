/**
 * workspaceStore —— 工作区 / 会话 / 风格 / 认证 / 主题（从 home.tsx 迁移）
 *
 * 职责：workspace 列表与 bootstrap、thread CRUD、style CRUD、
 * 认证守卫、主题切换、面板切换。
 *
 * 被 executionStore 通过 ExecutionDeps 间接调用（getActiveThreadId 等）。
 */
import { create } from "zustand";
import { toast } from "sonner";
import type { Style, ThreadSummary, WorkspacePanel, WorkspaceSummary } from "@/lib/types";
import {
  activateStyle as activateStyleRequest,
  createStyle as createStyleRequest,
  createThread as createThreadRequest,
  createWorkspace as createWorkspaceRequest,
  deleteStyle as deleteStyleRequest,
  deleteThread as deleteThreadRequest,
  deleteWorkspace as deleteWorkspaceRequest,
  fetchInit,
  fetchWorkspaceBootstrap,
  fetchMeOrNull,
  logout as logoutRequest,
  optimizeStyle as optimizeStyleRequest,
  updateStyle as updateStyleRequest,
  updateThread as updateThreadRequest,
  fetchWorkspaces,
} from "@/lib/api";

// 内容面板数据类型（bootstrap/switchWorkspace 返回，供 contentStore 消费）
export interface ContentData {
  outlineMarkdown: string;
  storylineMarkdown: string;
  storylineEntries: { filename: string; title: string; markdown: string }[];
  activeStorylineFilename: string;
  worldviewMarkdown: string;
  detailOutlineChapters: { filename: string; title: string; markdown: string }[];
  activeDetailChapterFilename: string;
  characters: { filename: string; name: string; markdown: string }[];
  activeCharacterFilename: string;
  novelChapters: { filename: string; title: string; markdown: string }[];
  activeNovelFilename: string;
}

type ThemeMode = "light" | "dark";
const themeStorageKey = "writer-theme";

interface WorkspaceState {
  // 认证
  authChecked: boolean;
  authUser: { username: string; is_admin: boolean } | null;
  hasApiKey: boolean;

  // 主题
  theme: ThemeMode;
  themeReady: boolean;

  // 面板
  activePanel: WorkspacePanel;

  // 工作区
  workspaces: WorkspaceSummary[];
  activeWorkspaceId: string;
  activeWorkspaceDomain: string;
  bootstrapping: boolean;
  creatingWorkspace: boolean;
  deletingWorkspace: boolean;

  // 会话
  threads: ThreadSummary[];
  activeThreadId: string;
  creatingThread: boolean;
  deleting: boolean;
  sessionMenuOpen: boolean;

  // 风格
  styles: Style[];
  styleModalOpen: boolean;
  creatingStyle: boolean;

  // 弹窗
  workspaceCreateOpen: boolean;
  newWorkspaceName: string;
  newWorkspaceDomain: "writing" | "image";
  workspaceDeleteOpen: boolean;
  pendingDeleteWorkspaceId: string;

  // actions
  checkAuth: () => Promise<boolean>;
  handleLogout: (navigate: (path: string, opts?: any) => void) => Promise<void>;
  initTheme: () => void;
  toggleTheme: () => void;
  bootstrap: () => Promise<ContentData | null>;
  switchWorkspace: (workspaceId: string) => Promise<ContentData | null>;
  handleCreateWorkspace: () => Promise<void>;
  handleDeleteWorkspace: () => Promise<void>;
  handleCreateThread: () => Promise<ThreadSummary | undefined>;
  handleSelectThread: (threadId: string) => void;
  handleDeleteThread: (threadId: string) => Promise<void>;

  // 风格 actions
  handleCreateStyle: (name: string, metaStyle: string, storybuildingStyle: string, detailOutlineStyle: string, writingStyle: string) => Promise<void>;
  handleUpdateStyle: (styleId: string, fields: Record<string, string>) => Promise<boolean>;
  handleOptimizeStyle: (styleType: string, content: string) => Promise<string>;
  handleDeleteStyle: (styleId: string) => Promise<void>;
  handleSelectStyle: (styleId: string | null) => Promise<void>;

  // 直接 setter（供 executionStore deps 用）
  setActiveThreadId: (id: string) => void;
  setActiveWorkspaceId: (id: string) => void;
  setThreads: (updater: ThreadSummary[] | ((current: ThreadSummary[]) => ThreadSummary[])) => void;
  setTheme: (theme: ThemeMode) => void;
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  authChecked: false,
  authUser: null,
  hasApiKey: false,
  theme: "light",
  themeReady: false,
  activePanel: "chat",
  workspaces: [],
  activeWorkspaceId: "",
  activeWorkspaceDomain: "writing",
  bootstrapping: false,
  creatingWorkspace: false,
  deletingWorkspace: false,
  threads: [],
  activeThreadId: "",
  creatingThread: false,
  deleting: false,
  sessionMenuOpen: false,
  styles: [],
  styleModalOpen: false,
  creatingStyle: false,
  workspaceCreateOpen: false,
  newWorkspaceName: "失忆编剧大纲",
  newWorkspaceDomain: "writing",
  workspaceDeleteOpen: false,
  pendingDeleteWorkspaceId: "",

  checkAuth: async () => {
    const me = await fetchMeOrNull();
    if (!me) return false;
    set({ authUser: { username: me.username, is_admin: me.is_admin }, hasApiKey: me.has_api_key, authChecked: true });
    return true;
  },

  handleLogout: async (navigate) => {
    try { await logoutRequest(); } catch { /* ignore */ }
    navigate("/login", { replace: true });
  },

  initTheme: () => {
    const stored = window.localStorage.getItem(themeStorageKey) === "dark" ? "dark" : "light";
    set({ theme: stored, themeReady: true });
  },

  toggleTheme: () => {
    const next = get().theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    window.localStorage.setItem(themeStorageKey, next);
    set({ theme: next });
  },

  bootstrap: async () => {
    try {
      set({ bootstrapping: true });
      const { workspaces: ws, styles: st } = await fetchInit();
      set({ workspaces: ws, styles: st });

      const firstWorkspaceId = ws[0]?.workspace_id || "";
      set({ activeWorkspaceId: firstWorkspaceId });
      if (!firstWorkspaceId) return null;

      return await loadWorkspaceData(firstWorkspaceId, set, get);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "初始化加载失败。");
      return null;
    } finally {
      set({ bootstrapping: false });
    }
  },

  switchWorkspace: async (workspaceId) => {
    return await loadWorkspaceData(workspaceId, set, get);
  },

  handleCreateWorkspace: async () => {
    const title = get().newWorkspaceName.trim();
    if (!title || get().creatingWorkspace) return;

    set({ creatingWorkspace: true });
    try {
      const workspace = await createWorkspaceRequest(title, get().newWorkspaceDomain);
      set({
        threads: [], activeThreadId: "",
        outlineMarkdown: "", storylineMarkdown: "", storylineEntries: [], activeStorylineFilename: "",
        worldviewMarkdown: "", detailOutlineChapters: [], activeDetailChapterFilename: "",
        characters: [], activeCharacterFilename: "", novelChapters: [], activeNovelFilename: "",
        workspaces: [workspace, ...get().workspaces],
        activeWorkspaceId: workspace.workspace_id,
        newWorkspaceName: "失忆编剧大纲",
        newWorkspaceDomain: "writing",
        workspaceCreateOpen: false,
      } as any); // 部分 content 字段由 contentStore 接管，这里只清 workspace 域
    } catch (createError) {
      toast.error(createError instanceof Error ? createError.message : "无法创建工作目录。");
    } finally {
      set({ creatingWorkspace: false });
    }
  },

  handleDeleteWorkspace: async () => {
    const pendingId = get().pendingDeleteWorkspaceId;
    if (!pendingId || get().deletingWorkspace) return;

    set({ deletingWorkspace: true });
    try {
      await deleteWorkspaceRequest(pendingId);
      const next = get().workspaces.filter((w) => w.workspace_id !== pendingId);
      if (pendingId === get().activeWorkspaceId) {
        set({ activeWorkspaceId: next[0]?.workspace_id || "" });
      }
      set({ workspaces: next, workspaceDeleteOpen: false, pendingDeleteWorkspaceId: "" });
    } catch (deleteError) {
      toast.error(deleteError instanceof Error ? deleteError.message : "无法删除工作目录。");
    } finally {
      set({ deletingWorkspace: false });
    }
  },

  handleCreateThread: async () => {
    const workspaceId = get().activeWorkspaceId;
    if (!workspaceId || get().creatingThread) return;
    set({ creatingThread: true });
    try {
      const thread = await createThreadRequest(workspaceId);
      set((state) => ({
        threads: [thread, ...state.threads.filter((item) => item.thread_id !== thread.thread_id)],
        activeThreadId: thread.thread_id,
      }));
      return thread;
    } catch (createError) {
      toast.error(createError instanceof Error ? createError.message : "无法创建新会话。");
    } finally {
      set({ creatingThread: false });
    }
  },

  handleSelectThread: (threadId) => {
    if (threadId === get().activeThreadId) return;
    set({ activeThreadId: threadId });
  },

  handleDeleteThread: async (threadId) => {
    if (!threadId || get().deleting) return;
    set({ deleting: true });
    try {
      await deleteThreadRequest(threadId);
      set((state) => {
        const next = state.threads.filter((thread) => thread.thread_id !== threadId);
        const patch: Partial<WorkspaceState> = { threads: next };
        if (state.activeThreadId === threadId) {
          patch.activeThreadId = next[0]?.thread_id || "";
        }
        return patch as WorkspaceState;
      });
    } catch (deleteError) {
      toast.error(deleteError instanceof Error ? deleteError.message : "删除失败。");
    } finally {
      set({ deleting: false });
    }
  },

  handleCreateStyle: async (name, metaStyle, storybuildingStyle, detailOutlineStyle, writingStyle) => {
    set({ creatingStyle: true });
    try {
      const style = await createStyleRequest(name, metaStyle, storybuildingStyle, detailOutlineStyle, writingStyle);
      set((state) => ({ styles: [...state.styles, style] }));
    } catch (createStyleError) {
      toast.error(createStyleError instanceof Error ? createStyleError.message : "无法创建风格。");
    } finally {
      set({ creatingStyle: false });
    }
  },

  handleUpdateStyle: async (styleId, fields) => {
    try {
      const updated = await updateStyleRequest(styleId, fields);
      set((state) => ({ styles: state.styles.map((s) => (s.style_id === updated.style_id ? updated : s)) }));
      return true;
    } catch (updateError) {
      toast.error(updateError instanceof Error ? updateError.message : "无法更新风格。");
      return false;
    }
  },

  handleOptimizeStyle: async (styleType, content) => {
    try {
      const result = await optimizeStyleRequest(styleType, content);
      return result.optimized;
    } catch (optimizeError) {
      toast.error(optimizeError instanceof Error ? optimizeError.message : "AI 优化失败。");
      return content;
    }
  },

  handleDeleteStyle: async (styleId) => {
    try {
      await deleteStyleRequest(styleId);
      set((state) => ({ styles: state.styles.filter((s) => s.style_id !== styleId) }));
    } catch (deleteStyleError) {
      toast.error(deleteStyleError instanceof Error ? deleteStyleError.message : "无法删除风格。");
    }
  },

  handleSelectStyle: async (styleId) => {
    const workspaceId = get().activeWorkspaceId;
    if (!workspaceId) return;
    try {
      const updated = await activateStyleRequest(workspaceId, styleId);
      set((state) => ({
        workspaces: state.workspaces.map((w) => (w.workspace_id === updated.workspace_id ? updated : w)),
      }));
    } catch (activateError) {
      toast.error(activateError instanceof Error ? activateError.message : "无法设置风格。");
    }
  },

  setActiveThreadId: (id) => set({ activeThreadId: id }),
  setActiveWorkspaceId: (id) => set({ activeWorkspaceId: id }),
  setThreads: (updater) =>
    set((state) => ({
      threads: typeof updater === "function" ? (updater as (c: ThreadSummary[]) => ThreadSummary[])(state.threads) : updater,
    })),
  setTheme: (theme) => set({ theme }),
}));

// ── bootstrap/switchWorkspace 共用的数据加载 ──
async function loadWorkspaceData(
  workspaceId: string,
  set: (partial: Partial<WorkspaceState>) => void,
  get: () => WorkspaceState,
): Promise<ContentData | null> {
  set({ bootstrapping: true } as any);
  try {
    const data = await fetchWorkspaceBootstrap(workspaceId);
    const activeThreadId = data.threads.some((t) => t.thread_id === get().activeThreadId)
      ? get().activeThreadId
      : data.threads[0]?.thread_id || "";

    set({
      threads: data.threads,
      activeThreadId,
      activeWorkspaceDomain: get().workspaces.find((w) => w.workspace_id === workspaceId)?.domain || "writing",
    });

    const content: ContentData = {
      outlineMarkdown: data.outline?.markdown || "",
      storylineMarkdown: data.storyline?.index_markdown || "",
      storylineEntries: data.storyline?.entries || [],
      activeStorylineFilename: data.storyline?.entries[0]?.filename || "",
      worldviewMarkdown: data.worldview?.markdown || "",
      detailOutlineChapters: data.detail_outline?.chapters || [],
      activeDetailChapterFilename: data.detail_outline?.chapters[0]?.filename || "",
      characters: data.characters?.characters || [],
      activeCharacterFilename: data.characters?.characters[0]?.filename || "",
      novelChapters: data.novel?.chapters || [],
      activeNovelFilename: data.novel?.chapters[0]?.filename || "",
    };
    return content;
  } catch (err) {
    toast.error(err instanceof Error ? err.message : "无法加载工作区数据。");
    return null;
  } finally {
    set({ bootstrapping: false } as any);
  }
}
