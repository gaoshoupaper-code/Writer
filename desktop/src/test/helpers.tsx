/**
 * 测试工具：home.tsx 集成测试的共享 fixture / mock / 渲染 wrapper。
 *
 * home.tsx 是一个 1826 行的上帝组件，挂载即触发大量副作用（路由守卫 →
 * bootstrap → trace 加载 → 面板轮询）。这里集中管理所有 mock，让各
 * T0a-T0d 测试文件只关注业务断言。
 */
import { vi } from "vitest";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { render } from "@testing-library/react";

// ── SSE chunk 编码工具 ──
// streamRequest 的 read() 返回 { done, value: Uint8Array }，
// value 是原始 SSE 文本字节。这里把 SSE 事件序列编码成 chunk 数组。
const encoder = new TextEncoder();

/**
 * 构造 SSE 事件文本行。
 * 一条 SSE 事件 = "event: <type>\ndata: <json>\n\n"
 */
export function sseEvent(type: string, data: unknown): string {
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

/**
 * 把多个 SSE 事件文本编码成 Uint8Array chunk 序列。
 * 每个 chunk 可以含 1~N 个事件（模拟真实流的分包）。
 *
 * 用法：
 *   const chunks = encodeChunks([
 *     sseEvent("trace_event", { type: "run_start", ... }),
 *     sseEvent("model_stream", { content: "hello" }),
 *     sseEvent("final", { markdown: "..." }),
 *   ]);
 */
export function encodeChunks(events: string[]): Uint8Array[] {
  return events.map((e) => encoder.encode(e));
}

/**
 * 把多个 SSE 事件合并成单个 chunk（简单场景用）。
 */
export function encodeSingleChunk(events: string[]): Uint8Array {
  return encoder.encode(events.join(""));
}

/**
 * 构造一个可控的 streamRequest mock。
 *
 * 调用 streamRequest() 后返回 { read, cancel }。
 * read() 依次吐出 chunks 里的 Uint8Array，最后返回 { done: true }。
 *
 * @param chunks 按 read() 顺序返回的字节序列
 * @returns streamRequest 的 mock 实现
 */
export function mockStreamRequest(chunks: Uint8Array[]) {
  let index = 0;
  let cancelled = false;

  const streamRequest = vi.fn().mockImplementation(() => {
    return Promise.resolve({
      read: () => {
        if (cancelled) return Promise.resolve({ done: true, value: undefined });
        if (index < chunks.length) {
          return Promise.resolve({ done: false, value: chunks[index++] });
        }
        return Promise.resolve({ done: true, value: undefined });
      },
      cancel: () => {
        cancelled = true;
        return Promise.resolve();
      },
    });
  });

  return streamRequest;
}

// ── Fixture 数据 ──

export const mockAuthMe = {
  user_id: "user-1",
  username: "testuser",
  is_admin: false,
  has_api_key: true,
};

export const mockWorkspace = {
  workspace_id: "ws-1",
  title: "测试工作目录",
  domain: "writing",
  workspace_path: "/test/workspace",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  session_count: 1,
  active_style_id: null,
};

export const mockThread = {
  thread_id: "thread-1",
  workspace_id: "ws-1",
  session_name: "会话 1",
  workspace_path: "/test/workspace",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

export const mockBootstrapEmpty = {
  threads: [mockThread],
  outline: null,
  storyline: null,
  detail_outline: null,
  characters: null,
  novel: null,
  worldview: null,
};

// ── 渲染 wrapper ──

/**
 * 用 MemoryRouter 包裹（home.tsx 顶层调 useNavigate，必须包在 Router 里）。
 */
export function renderWithRouter(ui: ReactElement) {
  return render(<MemoryRouter initialEntries={["/"]}>{ui}</MemoryRouter>);
}

/**
 * 重置所有 Zustand store 到初始状态。
 *
 * Zustand store 是全局单例——测试之间状态会残留。
 * 在 beforeEach 里调用此函数确保每个测试从干净状态开始。
 */
export async function resetStores() {
  const { useExecutionStore } = await import("@/stores/execution");
  const { useWorkspaceStore } = await import("@/stores/workspace");
  const { useTraceStore } = await import("@/stores/trace");
  const { useContentStore } = await import("@/stores/content");

  useExecutionStore.setState({
    messages: [{ role: "assistant", content: "先选择一个工作目录，再开启或恢复创作会话。" }],
    prompt: "", loading: false, result: null, activeReasoning: "",
    hasHistory: false, streamReader: null, threadMessages: new Map(),
  });
  useWorkspaceStore.setState({
    authChecked: false, authUser: null, hasApiKey: false,
    theme: "light", themeReady: false, activePanel: "chat",
    workspaces: [], activeWorkspaceId: "", activeWorkspaceDomain: "writing",
    bootstrapping: false, creatingWorkspace: false, deletingWorkspace: false,
    threads: [], activeThreadId: "", creatingThread: false, deleting: false,
    sessionMenuOpen: false, styles: [], styleModalOpen: false, creatingStyle: false,
    workspaceCreateOpen: false, newWorkspaceName: "失忆编剧大纲", newWorkspaceDomain: "writing",
    workspaceDeleteOpen: false, pendingDeleteWorkspaceId: "",
  });
  useTraceStore.setState({
    traceRuns: [], activeTraceId: "", liveTraceId: "", traceDetail: null,
    historyDetails: new Map(), traceLoading: false, deletingTraceId: "",
  });
  useContentStore.setState({
    outlineMarkdown: "", outlineLoading: false,
    detailOutlineChapters: [], detailOutlineLoading: false, activeDetailChapterFilename: "",
    novelChapters: [], activeNovelFilename: "", novelLoading: false,
    characters: [], charactersLoading: false, activeCharacterFilename: "",
    worldviewMarkdown: "", worldviewLoading: false,
    storylineMarkdown: "", storylineEntries: [], activeStorylineFilename: "",
  });
}
