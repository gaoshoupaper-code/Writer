/**
 * T0a 集成测试：发消息 → SSE → completed 全流程
 *
 * 验证路径（home.tsx performSubmit）：
 *   1. 用户在 ChatPanel 输入 prompt 并提交
 *   2. 追加 user message + 占位 assistant message
 *   3. SSE 流开启，model_stream 逐 chunk 累积
 *   4. trace_event run_start 绑定 traceId
 *   5. final 事件 → assistant message 标记 status: completed
 *
 * 这是 P1 重构的回归基线——重构后此测试必须仍然通过。
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";

// ── 共享 mock 工具 ──
const encoder = new TextEncoder();
function sseEvent(type: string, data: unknown): string {
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

// SSE 流：run_start → model_stream ×2 → final
const SSE_CHUNKS: Uint8Array[] = [
  sseEvent("trace_event", {
    trace_id: "trace-1",
    event_id: "evt-1",
    sequence: 1,
    type: "run_start",
    status: "running",
    timestamp: "2026-01-01T00:00:00Z",
    source: "system",
    input: { workspace_id: "ws-1", thread_id: "thread-1", session_name: "测试会话", endpoint: "screenplay.generate.stream" },
  }),
  sseEvent("model_stream", { content: "第一段正文" }),
  sseEvent("model_stream", { content: "第二段正文" }),
  sseEvent("final", {
    mode: "screenplay",
    thread_id: "thread-1",
    workspace_id: "ws-1",
    session_name: "测试会话",
    workspace_path: "/test",
    title: "测试",
    content: "第一段正文第二段正文",
    logline: "",
    synopsis: "",
    beats: [],
    markdown: "第一段正文第二段正文",
    evaluation_markdown: "",
  }),
].map((e) => encoder.encode(e));

// ── streamRequest mock（内联实现，不受 clearAllMocks 影响）──
// 每次调用返回新的 { read, cancel }，read 按序吐出 SSE_CHUNKS。
vi.mock("@/lib/stream", () => ({
  streamRequest: vi.fn().mockImplementation(() => {
    let index = 0;
    return Promise.resolve({
      read: () => {
        if (index < SSE_CHUNKS.length) {
          return Promise.resolve({ done: false, value: SSE_CHUNKS[index++] });
        }
        return Promise.resolve({ done: true, value: undefined });
      },
      cancel: () => Promise.resolve(),
    });
  }),
}));

// ── api mock ──
vi.mock("@/lib/api", () => ({
  API_BASE_URL: "",
  fetchMeOrNull: vi.fn().mockResolvedValue({ user_id: "u1", username: "test", is_admin: false, has_api_key: true }),
  fetchInit: vi.fn().mockResolvedValue({
    workspaces: [{ workspace_id: "ws-1", title: "测试", domain: "writing", workspace_path: "/test",
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", session_count: 1, active_style_id: null }],
    styles: [],
  }),
  fetchWorkspaceBootstrap: vi.fn().mockResolvedValue({
    // session_name 不以 "会话 " 开头，避免触发 updateThread 重命名逻辑
    threads: [{ thread_id: "thread-1", workspace_id: "ws-1", session_name: "测试会话", workspace_path: "/test",
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }],
    outline: null, storyline: null, detail_outline: null, characters: null, novel: null, worldview: null,
  }),
  fetchThreadTraces: vi.fn().mockResolvedValue([]),
  fetchTraceDetail: vi.fn().mockResolvedValue(null),
  createThread: vi.fn().mockResolvedValue({ thread_id: "thread-1", workspace_id: "ws-1", session_name: "会话 1", workspace_path: "/test", created_at: "", updated_at: "" }),
  updateThread: vi.fn().mockResolvedValue({ thread_id: "thread-1", workspace_id: "ws-1", session_name: "renamed", workspace_path: "/test", created_at: "", updated_at: "" }),
  deleteThread: vi.fn().mockResolvedValue({}),
  createWorkspace: vi.fn(), deleteWorkspace: vi.fn(),
  activateStyle: vi.fn(), createStyle: vi.fn(), updateStyle: vi.fn(), deleteStyle: vi.fn(), optimizeStyle: vi.fn(),
  deleteTrace: vi.fn(), logout: vi.fn(), trackCopy: vi.fn(), trackRegenerate: vi.fn(),
  workspaceNovelPdfUrl: vi.fn().mockReturnValue(""), workspaceNovelWordUrl: vi.fn().mockReturnValue(""),
}));

vi.mock("@/lib/usePanelPolling", () => ({ usePanelPolling: () => {} }));

// ── 子组件 mock（passthrough）──
vi.mock("@/components/workspace/AppShell", () => ({ AppShell: ({ children }: { children: ReactNode }) => <div>{children}</div> }));
vi.mock("@/components/workspace/TopBar", () => ({ TopBar: () => null }));
vi.mock("@/components/workspace/Sidebar", () => ({ Sidebar: () => null }));
vi.mock("@/components/workspace/StyleModal", () => ({ StyleModal: () => null }));
vi.mock("@/components/workspace/ConfirmDialog", () => ({ ConfirmDialog: () => null }));
vi.mock("@/components/workspace/TracePanel", () => ({ TracePanel: () => null }));
vi.mock("@/components/workspace/NovelPanel", () => ({ NovelPanel: () => null }));
vi.mock("@/components/workspace/ScriptPanel", () => ({ ScriptPanel: () => null }));
vi.mock("@/components/workspace/DetailOutlinePanel", () => ({ DetailOutlinePanel: () => null }));
vi.mock("@/components/workspace/CharactersPanel", () => ({ CharactersPanel: () => null }));
vi.mock("@/components/workspace/WorldviewPanel", () => ({ WorldviewPanel: () => null }));
vi.mock("@/components/workspace/StorylinePanel", () => ({ StorylinePanel: () => null }));
vi.mock("@/components/workspace/InterviewOptions", () => ({ InterviewOptions: () => null }));
vi.mock("@/components/workspace/ImageReviewCard", () => ({ ImageReviewCard: () => null }));
vi.mock("@/components/workspace/SessionMenu", () => ({ SessionMenu: () => null }));

// ── Import（在所有 mock 之后）──
const { MemoryRouter } = await import("react-router-dom");
const { render } = await import("@testing-library/react");
const Home = (await import("@/pages/home")).default;

function renderHome() {
  return render(<MemoryRouter initialEntries={["/"]}><Home /></MemoryRouter>);
}

describe("T0a: 发消息 → SSE → completed 全流程", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    // 重置 Zustand 全局 store（单例，测试间会残留状态）
    const { resetStores } = await import("./helpers");
    await resetStores();
  });

  it("用户发送消息后，SSE 流式累积正文，final 后标记 completed", async () => {
    const user = userEvent.setup();
    renderHome();

    // 等待 bootstrap 完成（auth + workspace + threads 加载），textarea 出现
    const input = await screen.findByRole("textbox", {}, { timeout: 10000 });

    // 输入并提交
    await user.type(input, "写一个关于勇气的故事");
    await user.keyboard("{Enter}");

    // SSE 完成后，assistant 消息应包含流式累积的正文
    await waitFor(() => {
      expect(screen.getByText(/第一段正文第二段正文/)).toBeInTheDocument();
    }, { timeout: 10000 });

    // 最终 assistant message 的 status 应是 completed
    const messages = document.querySelectorAll(".message.assistant");
    expect(messages.length).toBeGreaterThanOrEqual(1);
    const lastAssistant = messages[messages.length - 1];
    expect(lastAssistant).toHaveAttribute("data-status", "completed");
  });

  it("streamRequest 被调用时携带正确的 body（prompt + thread_id）", async () => {
    const { streamRequest } = await import("@/lib/stream");
    const user = userEvent.setup();
    renderHome();

    const input = await screen.findByRole("textbox", {}, { timeout: 10000 });
    await user.type(input, "测试prompt");
    await user.keyboard("{Enter}");

    await waitFor(() => {
      expect(streamRequest).toHaveBeenCalledWith(
        "/api/screenplay/generate/stream",
        expect.objectContaining({
          method: "POST",
          body: expect.objectContaining({
            thread_id: "thread-1",
            prompt: "测试prompt",
          }),
        }),
      );
    }, { timeout: 10000 });
  });
});
