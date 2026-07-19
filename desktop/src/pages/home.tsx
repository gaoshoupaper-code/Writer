import { FormEvent, useEffect, useMemo, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { AppShell } from "@/components/workspace/AppShell";
import { CharactersPanel } from "@/components/workspace/CharactersPanel";
import { ChatPanel } from "@/components/workspace/ChatPanel";
import { ConfirmDialog } from "@/components/workspace/ConfirmDialog";
import { DetailOutlinePanel } from "@/components/workspace/DetailOutlinePanel";
import { NovelPanel } from "@/components/workspace/NovelPanel";
import { ScriptPanel } from "@/components/workspace/ScriptPanel";
import { StorylinePanel } from "@/components/workspace/StorylinePanel";
import { Sidebar } from "@/components/workspace/Sidebar";
import { StyleModal } from "@/components/workspace/StyleModal";
import { TopBar } from "@/components/workspace/TopBar";
import { TracePanel } from "@/components/workspace/TracePanel";
import { WorldviewPanel } from "@/components/workspace/WorldviewPanel";
import {
  API_BASE_URL,
  apiFetch,
  trackCopy,
  workspaceNovelPdfUrl,
  workspaceNovelWordUrl,
} from "@/lib/api";
import { projectStageFlow } from "@/lib/stage";
import { usePanelPolling } from "@/lib/usePanelPolling";
import type { StageFlow } from "@/lib/stage";
import type { WorkspacePanel } from "@/lib/types";
import { useExecutionStore, setExecutionDeps } from "@/stores/execution";
import { useWorkspaceStore } from "@/stores/workspace";
import { useTraceStore } from "@/stores/trace";
import { useContentStore } from "@/stores/content";

const initialAssistantMessage = {
  role: "assistant" as const,
  content: "先选择一个工作目录，再开启或恢复创作会话。",
};

export default function Home() {
  const navigate = useNavigate();

  // ── 从 stores 取数据（替代原来的 ~55 个 useState）──
  // workspaceStore
  const authChecked = useWorkspaceStore((s) => s.authChecked);
  const authUser = useWorkspaceStore((s) => s.authUser);
  const hasApiKey = useWorkspaceStore((s) => s.hasApiKey);
  const theme = useWorkspaceStore((s) => s.theme);
  const themeReady = useWorkspaceStore((s) => s.themeReady);
  const activePanel = useWorkspaceStore((s) => s.activePanel);
  const setActivePanel = (panel: WorkspacePanel) => useWorkspaceStore.setState({ activePanel: panel });
  const workspaces = useWorkspaceStore((s) => s.workspaces);
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);
  const threads = useWorkspaceStore((s) => s.threads);
  const activeThreadId = useWorkspaceStore((s) => s.activeThreadId);
  const activeWorkspace = useMemo(
    () => workspaces.find((w) => w.workspace_id === activeWorkspaceId) ?? null,
    [workspaces, activeWorkspaceId],
  );
  const activeThread = useMemo(
    () => threads.find((t) => t.thread_id === activeThreadId) ?? null,
    [threads, activeThreadId],
  );
  const styles = useWorkspaceStore((s) => s.styles);
  const activeStyleName = useMemo(() => {
    const activeStyleId = activeWorkspace?.active_style_id;
    if (!activeStyleId) return null;
    return styles.find((s) => s.style_id === activeStyleId)?.name ?? null;
  }, [activeWorkspace?.active_style_id, styles]);
  const bootstrapping = useWorkspaceStore((s) => s.bootstrapping);
  const creatingWorkspace = useWorkspaceStore((s) => s.creatingWorkspace);
  const deletingWorkspace = useWorkspaceStore((s) => s.deletingWorkspace);
  const creatingThread = useWorkspaceStore((s) => s.creatingThread);
  const deleting = useWorkspaceStore((s) => s.deleting);
  const workspaceCreateOpen = useWorkspaceStore((s) => s.workspaceCreateOpen);
  const newWorkspaceName = useWorkspaceStore((s) => s.newWorkspaceName);
  const newWorkspaceDomain = useWorkspaceStore((s) => s.newWorkspaceDomain);
  const workspaceDeleteOpen = useWorkspaceStore((s) => s.workspaceDeleteOpen);
  const pendingDeleteWorkspaceId = useWorkspaceStore((s) => s.pendingDeleteWorkspaceId);
  const sessionMenuOpen = useWorkspaceStore((s) => s.sessionMenuOpen);
  const styleModalOpen = useWorkspaceStore((s) => s.styleModalOpen);
  const creatingStyle = useWorkspaceStore((s) => s.creatingStyle);

  // executionStore
  const messages = useExecutionStore((s) => s.messages);
  const prompt = useExecutionStore((s) => s.prompt);
  const loading = useExecutionStore((s) => s.loading);
  const liveTraceId = useTraceStore((s) => s.liveTraceId);

  // traceStore
  const traceRuns = useTraceStore((s) => s.traceRuns);
  const activeTraceId = useTraceStore((s) => s.activeTraceId);
  const traceDetail = useTraceStore((s) => s.traceDetail);
  const historyDetails = useTraceStore((s) => s.historyDetails);
  const traceLoading = useTraceStore((s) => s.traceLoading);
  const deletingTraceId = useTraceStore((s) => s.deletingTraceId);
  const stoppingTraceId = useTraceStore((s) => s.stoppingTraceId);

  // contentStore
  const outlineMarkdown = useContentStore((s) => s.outlineMarkdown);
  const outlineLoading = useContentStore((s) => s.outlineLoading);
  const detailOutlineChapters = useContentStore((s) => s.detailOutlineChapters);
  const detailOutlineLoading = useContentStore((s) => s.detailOutlineLoading);
  const activeDetailChapterFilename = useContentStore((s) => s.activeDetailChapterFilename);
  const novelChapters = useContentStore((s) => s.novelChapters);
  const activeNovelFilename = useContentStore((s) => s.activeNovelFilename);
  const novelLoading = useContentStore((s) => s.novelLoading);
  const characters = useContentStore((s) => s.characters);
  const charactersLoading = useContentStore((s) => s.charactersLoading);
  const activeCharacterFilename = useContentStore((s) => s.activeCharacterFilename);
  const worldviewMarkdown = useContentStore((s) => s.worldviewMarkdown);
  const worldviewLoading = useContentStore((s) => s.worldviewLoading);
  const storylineMarkdown = useContentStore((s) => s.storylineMarkdown);
  const storylineEntries = useContentStore((s) => s.storylineEntries);
  const activeStorylineFilename = useContentStore((s) => s.activeStorylineFilename);

  const aiDisabled = !hasApiKey;
  const workspacePath = activeWorkspace?.workspace_path ?? activeThread?.workspace_path;
  const result = useExecutionStore((s) => s.result);
  const currentOutlineMarkdown = result?.thread_id === activeThreadId && result.markdown?.trim() ? result.markdown : outlineMarkdown;

  // ── 注入 executionDeps（连接三个 store）──
  // 只在首次挂载时注入一次。deps 引用的是 store 的 getState/setState，始终拿到最新值。
  const depsInjected = useRef(false);
  useEffect(() => {
    if (depsInjected.current) return;
    depsInjected.current = true;
    setExecutionDeps({
      getActiveThreadId: () => useWorkspaceStore.getState().activeThreadId,
      getActiveWorkspaceId: () => useWorkspaceStore.getState().activeWorkspaceId,
      getActiveThreadSessionName: () => {
        const ws = useWorkspaceStore.getState();
        return ws.threads.find((t) => t.thread_id === ws.activeThreadId)?.session_name ?? "";
      },
      getActiveThreadWorkspacePath: () => {
        const ws = useWorkspaceStore.getState();
        return ws.threads.find((t) => t.thread_id === ws.activeThreadId)?.workspace_path ?? "";
      },
      getActiveWorkspaceDomain: () => {
        const ws = useWorkspaceStore.getState();
        return ws.workspaces.find((w) => w.workspace_id === ws.activeWorkspaceId)?.domain ?? "writing";
      },
      setActiveThreadId: (id) => useWorkspaceStore.getState().setActiveThreadId(id),
      setThreads: (updater) => useWorkspaceStore.getState().setThreads(updater as any),
      addThread: (thread) => useWorkspaceStore.getState().setThreads((current) => [thread, ...current.filter((t) => t.thread_id !== thread.thread_id)]),
      getTraceRuns: () => useTraceStore.getState().traceRuns,
      getActiveTraceId: () => useTraceStore.getState().activeTraceId,
      getLiveTraceId: () => useTraceStore.getState().liveTraceId,
      setTraceRuns: (updater) => useTraceStore.getState().setTraceRuns(updater as any),
      setTraceDetail: (updater) => useTraceStore.getState().setTraceDetail(updater as any),
      setActiveTraceId: (id) => useTraceStore.getState().setActiveTraceId(id),
      setLiveTraceId: (id) => useTraceStore.getState().setLiveTraceId(id),
    });
  }, []);

  // ── 主题 ──
  useEffect(() => { useWorkspaceStore.getState().initTheme(); }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    if (themeReady) window.localStorage.setItem("writer-theme", theme);
  }, [theme, themeReady]);

  // ── 路由守卫 ──
  useEffect(() => {
    let ignore = false;
    (async () => {
      const ok = await useWorkspaceStore.getState().checkAuth();
      if (ignore || !ok) {
        if (!ignore) navigate("/login", { replace: true });
        return;
      }
    })();
    return () => { ignore = true; };
  }, [navigate]);

  // ── 首次 bootstrap ──
  const initialBootDone = useRef(false);
  const skipNextBootstrap = useRef(false);
  useEffect(() => {
    let ignore = false;
    (async () => {
      const content = await useWorkspaceStore.getState().bootstrap();
      if (ignore || !content) return;
      useContentStore.getState().setContentData(content);
    })();
    return () => { ignore = true; };
  }, []);

  // ── 后续切换工作区 ──
  useEffect(() => {
    if (!activeWorkspaceId) return;
    if (!initialBootDone.current) { initialBootDone.current = true; return; }
    if (skipNextBootstrap.current) { skipNextBootstrap.current = false; return; }
    let ignore = false;
    (async () => {
      useContentStore.setState({
        outlineLoading: true, detailOutlineLoading: true, charactersLoading: true,
        novelLoading: true, worldviewLoading: true,
      });
      const content = await useWorkspaceStore.getState().switchWorkspace(activeWorkspaceId);
      if (ignore || !content) return;
      useContentStore.getState().setContentData(content);
    })();
    return () => { ignore = true; };
  }, [activeWorkspaceId]);

  // ── 会话切换：存取消息 ──
  const prevThreadIdRef = useRef(activeThreadId);
  useEffect(() => {
    const prevId = prevThreadIdRef.current;
    if (prevId && prevId !== activeWorkspaceId) {
      useExecutionStore.getState().threadMessages.set(prevId, useExecutionStore.getState().messages);
    }
  }, [activeWorkspaceId]);

  useEffect(() => {
    const prevId = prevThreadIdRef.current;
    if (prevId === activeThreadId) return;
    // 保存旧 thread 消息
    const exec = useExecutionStore.getState();
    if (prevId) exec.threadMessages.set(prevId, exec.messages);
    // 加载新 thread 消息
    exec.loadThreadMessages(activeThreadId);
    prevThreadIdRef.current = activeThreadId;
  }, [activeThreadId]);

  // ── trace 加载（切换 thread 时）──
  useEffect(() => {
    if (!activeThreadId) {
      useTraceStore.getState().clearTrace();
      return;
    }
    useTraceStore.getState().loadTraceRuns(activeThreadId);
  }, [activeThreadId]);

  // ── trace detail 加载（切换 activeTraceId 时）──
  useEffect(() => {
    if (!activeThreadId || !activeTraceId) {
      useTraceStore.getState().setTraceDetail(() => null);
      return;
    }
    useTraceStore.getState().loadTraceDetail(activeThreadId, activeTraceId);
  }, [activeThreadId, activeTraceId, liveTraceId]);

  // ── 历史回放：加载本会话所有 trace 的 detail ──
  const historyTraceKey = traceRuns.map((r) => r.trace_id).join(",");
  useEffect(() => {
    if (!activeThreadId || !historyTraceKey) {
      useTraceStore.setState({ historyDetails: new Map() });
      return;
    }
    const ids = historyTraceKey.split(",").filter(Boolean);
    useTraceStore.getState().loadHistoryDetails(activeThreadId, ids);
  }, [activeThreadId, historyTraceKey]);

  // ── 面板轮询 ──
  usePanelPolling({
    activeWorkspaceId,
    activePanel,
    loading,
    bootstrapping,
    setters: {
      setNovelChapters: (v) => useContentStore.getState().setNovelChapters(v),
      setActiveNovelFilename: (fn) => useContentStore.setState((s) => ({ activeNovelFilename: fn(s.activeNovelFilename) })),
      setNovelLoading: (v) => useContentStore.getState().setNovelLoading(v),
      setStorylineMarkdown: (v) => useContentStore.getState().setStorylineMarkdown(v),
      setStorylineEntries: (v) => useContentStore.getState().setStorylineEntries(v),
      setActiveStorylineFilename: (fn) => useContentStore.setState((s) => ({ activeStorylineFilename: fn(s.activeStorylineFilename) })),
      setDetailOutlineChapters: (v) => useContentStore.getState().setDetailOutlineChapters(v),
      setActiveDetailChapterFilename: (fn) => useContentStore.setState((s) => ({ activeDetailChapterFilename: fn(s.activeDetailChapterFilename) })),
      setDetailOutlineLoading: (v) => useContentStore.getState().setDetailOutlineLoading(v),
      setCharacters: (v) => useContentStore.getState().setCharacters(v),
      setActiveCharacterFilename: (fn) => useContentStore.setState((s) => ({ activeCharacterFilename: fn(s.activeCharacterFilename) })),
      setCharactersLoading: (v) => useContentStore.getState().setCharactersLoading(v),
      setWorldviewMarkdown: (v) => useContentStore.getState().setWorldviewMarkdown(v),
      setWorldviewLoading: (v) => useContentStore.getState().setWorldviewLoading(v),
      setOutlineMarkdown: (v) => useContentStore.getState().setOutlineMarkdown(v),
      setOutlineLoading: (v) => useContentStore.getState().setOutlineLoading(v),
    },
  });

  // ── stageFlow 派生（从 traceDetail + messages 派生）──
  const stageFlow = useMemo<StageFlow | null>(() => {
    if (!traceDetail) return null;
    const owner = messages.findLast((m) => m.role === "assistant" && m.traceId === traceDetail.run.trace_id);
    return projectStageFlow(traceDetail, owner?.tools ?? []);
  }, [traceDetail, messages]);

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

  // ── 事件处理（委托 store action）──
  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (aiDisabled) {
      toast.error("请先在设置页填写你的 API Key，才能使用 AI 生成。");
      return;
    }
    try {
      if (activeWorkspace?.domain === "image") {
        await useExecutionStore.getState().submitImage({ prompt });
        useExecutionStore.getState().setPrompt("");
        return;
      }
      await useExecutionStore.getState().submit(prompt);
      useExecutionStore.getState().setPrompt("");
    } catch {
      /* performSubmit 内部已处理 UI + toast */
    }
  }

  function handleStopGeneration() {
    useExecutionStore.getState().stop();
  }

  function handleRetry() {
    useExecutionStore.getState().retry();
  }

  async function handleCreateWorkspace() {
    skipNextBootstrap.current = true;
    // 清空执行消息
    useExecutionStore.getState().resetMessages();
    useExecutionStore.getState().clearThreadMessages();
    useContentStore.getState().clearContent();
    await useWorkspaceStore.getState().handleCreateWorkspace();
  }

  async function handleDeleteWorkspace() {
    const pendingId = useWorkspaceStore.getState().pendingDeleteWorkspaceId;
    await useWorkspaceStore.getState().handleDeleteWorkspace();
    // 如果删的是当前工作目录，清空面板
    if (pendingId === activeWorkspaceId) {
      useExecutionStore.getState().clearThreadMessages();
      useExecutionStore.getState().resetMessages();
      useContentStore.getState().clearContent();
      useTraceStore.getState().clearTrace();
      useWorkspaceStore.setState({ sessionMenuOpen: false });
    }
  }

  async function handleCreateThread() {
    // 保存当前会话消息
    if (activeThreadId) {
      const exec = useExecutionStore.getState();
      exec.threadMessages.set(activeThreadId, exec.messages);
    }
    await useWorkspaceStore.getState().handleCreateThread();
    useExecutionStore.getState().resetMessages();
    useExecutionStore.setState({ result: null });
  }

  function handleSelectThread(threadId: string) {
    useWorkspaceStore.getState().handleSelectThread(threadId);
  }

  async function handleDeleteThread(threadId: string) {
    // 清理 threadMessages
    useExecutionStore.getState().threadMessages.delete(threadId);
    await useWorkspaceStore.getState().handleDeleteThread(threadId);
    if (activeThreadId === threadId) {
      useTraceStore.getState().clearTrace();
    }
  }

  async function handleDeleteTrace(traceId: string) {
    await useTraceStore.getState().deleteTrace(activeThreadId, traceId);
  }

  function handleLogout() {
    useWorkspaceStore.getState().handleLogout(navigate);
  }

  // 未登录：不渲染主界面
  if (!authChecked) return null;

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
            onWorkspaceChange={(id) => useWorkspaceStore.getState().setActiveWorkspaceId(id)}
            onCreateWorkspace={() => useWorkspaceStore.setState({ workspaceCreateOpen: true })}
            onDeleteWorkspace={(workspaceId) =>
              useWorkspaceStore.setState({ pendingDeleteWorkspaceId: workspaceId, workspaceDeleteOpen: true })
            }
            onThemeToggle={() => useWorkspaceStore.getState().toggleTheme()}
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
            onPromptChange={(p) => useExecutionStore.getState().setPrompt(p)}
            onSubmit={handleSubmit}
            onResumeSubmit={async (resumeText) => {
              if (aiDisabled) { toast.error("请先在设置页填写你的 API Key，才能使用 AI 生成。"); return; }
              await useExecutionStore.getState().resume(resumeText);
            }}
            onImageReviewSubmit={async (resume) => {
              if (aiDisabled) { toast.error("请先在设置页填写你的 API Key，才能使用 AI 生成。"); return; }
              await useExecutionStore.getState().submitImage({ resume });
            }}
            onStop={handleStopGeneration}
            onToggleSessionMenu={() => useWorkspaceStore.setState((s) => ({ sessionMenuOpen: !s.sessionMenuOpen }))}
            onCloseSessionMenu={() => useWorkspaceStore.setState({ sessionMenuOpen: false })}
            onCreateThread={handleCreateThread}
            onSelectThread={handleSelectThread}
            onDeleteThread={handleDeleteThread}
            onOpenStyleModal={() => useWorkspaceStore.setState({ styleModalOpen: true })}
            stageFlows={stageFlows}
            onRetry={handleRetry}
          />
        ) : null}

        {activePanel === "novel" ? (
          <NovelPanel
            chapters={novelChapters}
            activeFilename={activeNovelFilename}
            loading={novelLoading}
            onSelectChapter={(f) => useContentStore.getState().setActiveNovelFilename(f)}
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
            onSelectStoryline={(f) => useContentStore.getState().setActiveStorylineFilename(f)}
          />
        ) : null}

        {activePanel === "detail_outline" ? (
          <DetailOutlinePanel
            chapters={detailOutlineChapters}
            activeFilename={activeDetailChapterFilename}
            loading={detailOutlineLoading}
            onSelectChapter={(f) => useContentStore.getState().setActiveDetailChapterFilename(f)}
          />
        ) : null}

        {activePanel === "characters" ? (
          <CharactersPanel
            characters={characters}
            activeFilename={activeCharacterFilename}
            loading={charactersLoading}
            onSelectCharacter={(f) => useContentStore.getState().setActiveCharacterFilename(f)}
          />
        ) : null}

        {activePanel === "worldview" ? (
          <WorldviewPanel workspacePath={workspacePath} markdown={worldviewMarkdown} loading={worldviewLoading} />
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
            stoppingTraceId={stoppingTraceId}
            onSelectTrace={(id) => useTraceStore.getState().setActiveTraceId(id)}
            onDeleteTrace={handleDeleteTrace}
            onStopTrace={(id) => {
              if (activeThreadId) {
                void useTraceStore.getState().stopTrace(activeThreadId, id);
              }
            }}
          />
        ) : null}
      </AppShell>

      {workspaceCreateOpen ? (
        <div className="modal-overlay" role="presentation">
          <section className="modal-content" role="dialog" aria-modal="true" aria-labelledby="workspace-create-title">
            <h2 className="modal-title" id="workspace-create-title">新建工作目录</h2>
            <p className="modal-description">选择类型并输入名称，创建后会直接切换。</p>
            <form
              className="workspace-create-form"
              onSubmit={(event) => { event.preventDefault(); handleCreateWorkspace(); }}
            >
              <div className="workspace-domain-select">
                <label className={`domain-option${newWorkspaceDomain === "writing" ? " selected" : ""}`}>
                  <input type="radio" name="workspace-domain" value="writing" checked={newWorkspaceDomain === "writing"} onChange={() => useWorkspaceStore.setState({ newWorkspaceDomain: "writing" })} disabled={creatingWorkspace} />
                  <span className="domain-option-body"><strong>✍️ 写作</strong><small>小说/剧本创作</small></span>
                </label>
                <label className={`domain-option${newWorkspaceDomain === "image" ? " selected" : ""}`}>
                  <input type="radio" name="workspace-domain" value="image" checked={newWorkspaceDomain === "image"} onChange={() => useWorkspaceStore.setState({ newWorkspaceDomain: "image" })} disabled={creatingWorkspace} />
                  <span className="domain-option-body"><strong>🎨 文生图</strong><small>图片生成与优化</small></span>
                </label>
              </div>
              <input
                className="thread-input workspace-create-input"
                value={newWorkspaceName}
                onChange={(event) => useWorkspaceStore.setState({ newWorkspaceName: event.target.value })}
                placeholder={newWorkspaceDomain === "image" ? "请输入图片主题名" : "请输入新剧本名"}
                autoFocus disabled={creatingWorkspace}
              />
              <div className="modal-actions">
                <button className="modal-button modal-cancel" type="button" onClick={() => useWorkspaceStore.setState({ workspaceCreateOpen: false })} disabled={creatingWorkspace}>取消</button>
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
        onCancel={() => useWorkspaceStore.setState({ workspaceDeleteOpen: false, pendingDeleteWorkspaceId: "" })}
      />

      {styleModalOpen ? (
        <StyleModal
          styles={styles}
          activeStyleId={activeWorkspace?.active_style_id ?? null}
          creating={creatingStyle}
          onCreateStyle={(name, meta, sb, dlo, w) => useWorkspaceStore.getState().handleCreateStyle(name, meta, sb, dlo, w)}
          onUpdateStyle={(id, fields) => useWorkspaceStore.getState().handleUpdateStyle(id, fields)}
          onDeleteStyle={(id) => useWorkspaceStore.getState().handleDeleteStyle(id)}
          onSelectStyle={(id) => useWorkspaceStore.getState().handleSelectStyle(id)}
          onOptimizeStyle={(type, content) => useWorkspaceStore.getState().handleOptimizeStyle(type, content)}
          onClose={() => useWorkspaceStore.setState({ styleModalOpen: false })}
        />
      ) : null}
    </>
  );
}
