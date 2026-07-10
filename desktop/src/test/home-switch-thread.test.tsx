/**
 * T0d 集成测试：切会话消息保留
 *
 * 验证路径（home.tsx threadMessagesRef 切换逻辑）：
 *   1. bootstrap 加载两个 thread（thread-1, thread-2）
 *   2. 在 thread-1 发消息并完成
 *   3. 切到 thread-2（消息列表变空/初始态）
 *   4. 切回 thread-1 → thread-1 的消息仍在
 *
 * home.tsx 用 threadMessagesRef（Map<threadId, ChatMessage[]>）在切换时存取。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";

const encoder = new TextEncoder();
function sseEvent(type: string, data: unknown): string {
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

const FINAL_CHUNKS: Uint8Array[] = [
  encoder.encode(sseEvent("trace_event", {
    trace_id: "trace-1", event_id: "evt-1", sequence: 1, type: "run_start", status: "running",
    timestamp: "2026-01-01T00:00:00Z", source: "system",
    input: { workspace_id: "ws-1", thread_id: "thread-1", session_name: "会话A", endpoint: "x" },
  })),
  encoder.encode(sseEvent("model_stream", { content: "会话A的正文" })),
  encoder.encode(sseEvent("final", {
    mode: "screenplay", thread_id: "thread-1", workspace_id: "ws-1", session_name: "会话A",
    workspace_path: "/test", title: "T", content: "会话A的正文", logline: "", synopsis: "",
    beats: [], markdown: "会话A的正文", evaluation_markdown: "",
  })),
];

vi.mock("@/lib/stream", () => ({
  streamRequest: vi.fn().mockImplementation(() => {
    let index = 0;
    return Promise.resolve({
      read: () => {
        if (index < FINAL_CHUNKS.length) return Promise.resolve({ done: false, value: FINAL_CHUNKS[index++] });
        return Promise.resolve({ done: true, value: undefined });
      },
      cancel: () => Promise.resolve(),
    });
  }),
}));

// 两个 thread
const THREADS = [
  { thread_id: "thread-1", workspace_id: "ws-1", session_name: "会话A", workspace_path: "/t", created_at: "", updated_at: "" },
  { thread_id: "thread-2", workspace_id: "ws-1", session_name: "会话B", workspace_path: "/t", created_at: "", updated_at: "" },
];

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "",
  fetchMeOrNull: vi.fn().mockResolvedValue({ user_id: "u1", username: "test", is_admin: false, has_api_key: true }),
  fetchInit: vi.fn().mockResolvedValue({
    workspaces: [{ workspace_id: "ws-1", title: "T", domain: "writing", workspace_path: "/t",
      created_at: "", updated_at: "", session_count: 2, active_style_id: null }],
    styles: [],
  }),
  fetchWorkspaceBootstrap: vi.fn().mockResolvedValue({
    threads: THREADS,
    outline: null, storyline: null, detail_outline: null, characters: null, novel: null, worldview: null,
  }),
  fetchThreadTraces: vi.fn().mockResolvedValue([]),
  fetchTraceDetail: vi.fn().mockResolvedValue(null),
  createThread: vi.fn(), updateThread: vi.fn(), deleteThread: vi.fn(),
  createWorkspace: vi.fn(), deleteWorkspace: vi.fn(),
  activateStyle: vi.fn(), createStyle: vi.fn(), updateStyle: vi.fn(), deleteStyle: vi.fn(), optimizeStyle: vi.fn(),
  deleteTrace: vi.fn(), logout: vi.fn(), trackCopy: vi.fn(), trackRegenerate: vi.fn(),
  workspaceNovelPdfUrl: () => "", workspaceNovelWordUrl: () => "",
}));

vi.mock("@/lib/usePanelPolling", () => ({ usePanelPolling: () => {} }));

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

// SessionMenu mock：渲染线程切换按钮
vi.mock("@/components/workspace/SessionMenu", () => ({
  SessionMenu: ({ threads, onSelectThread }: { threads: { thread_id: string; session_name: string }[]; onSelectThread: (id: string) => void }) => (
    <div data-testid="session-menu">
      {threads.map((t) => (
        <button key={t.thread_id} type="button" data-thread-id={t.thread_id} onClick={() => onSelectThread(t.thread_id)}>
          {t.session_name}
        </button>
      ))}
    </div>
  ),
}));

const { MemoryRouter } = await import("react-router-dom");
const { render } = await import("@testing-library/react");
const Home = (await import("@/pages/home")).default;

describe("T0d: 切会话消息保留", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { resetStores } = await import("./helpers");
    await resetStores();
  });

  it("在会话A发消息后切到B再切回A，A的消息仍在", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/"]}><Home /></MemoryRouter>);

    const input = await screen.findByRole("textbox", {}, { timeout: 10000 });

    // 在 thread-1（会话A）发消息
    await user.type(input, "在会话A写的消息");
    await user.keyboard("{Enter}");

    // 等 SSE 完成
    await waitFor(() => {
      expect(screen.getByText("会话A的正文")).toBeInTheDocument();
    }, { timeout: 10000 });

    // 切到 thread-2（会话B）
    await user.click(screen.getByText("会话B"));

    // 会话B 应显示初始消息（不含会话A的内容）
    await waitFor(() => {
      expect(screen.queryByText("会话A的正文")).not.toBeInTheDocument();
    }, { timeout: 5000 });

    // 切回 thread-1（会话A）
    await user.click(screen.getByText("会话A"));

    // 会话A 的消息应恢复
    await waitFor(() => {
      expect(screen.getByText("会话A的正文")).toBeInTheDocument();
    }, { timeout: 5000 });
  });
});
