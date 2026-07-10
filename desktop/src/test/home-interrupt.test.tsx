/**
 * T0b 集成测试：HITL interrupt → resume
 *
 * 验证路径（home.tsx performSubmit interrupt 分支）：
 *   1. 用户发送消息 → SSE 推 interrupt 事件（带 question + options）
 *   2. assistant 消息进入 awaitingInput 态，渲染选项
 *   3. 用户选择选项提交 → resume（body 含 resume + trace_id）
 *   4. 第二次 SSE → final → completed
 *
 * 注意：streamRequest 被调用两次——第一次返回 interrupt 流，第二次返回 final 流。
 * 用 mockImplementation 按调用序号切换不同的 SSE 数据。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";

const encoder = new TextEncoder();
function sseEvent(type: string, data: unknown): string {
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}
function encode(events: string[]): Uint8Array[] {
  return events.map((e) => encoder.encode(e));
}

// 第一次调用：run_start → interrupt（HITL choice）
const INTERRUPT_CHUNKS = encode([
  sseEvent("trace_event", {
    trace_id: "trace-1", event_id: "evt-1", sequence: 1, type: "run_start", status: "running",
    timestamp: "2026-01-01T00:00:00Z", source: "system",
    input: { workspace_id: "ws-1", thread_id: "thread-1", session_name: "测试会话", endpoint: "screenplay.generate.stream" },
  }),
  sseEvent("interrupt", {
    kind: "choice",
    question: "第 3 章主角是否遇到反派？",
    options: [
      { label: "遇到，加些冲突", description: "让主角在此章遇到反派" },
      { label: "先铺垫情绪", description: "下章再遇反派" },
    ],
    multi_select: false,
    source: "writing-subagent",
  }),
]);

// 第二次调用（resume）：final
const FINAL_CHUNKS = encode([
  sseEvent("final", {
    mode: "screenplay", thread_id: "thread-1", workspace_id: "ws-1", session_name: "测试会话",
    workspace_path: "/test", title: "T", content: "正文完成", logline: "", synopsis: "",
    beats: [], markdown: "正文完成", evaluation_markdown: "",
  }),
]);

// streamRequest 按调用序号返回不同的流
let callCount = 0;
vi.mock("@/lib/stream", () => ({
  streamRequest: vi.fn().mockImplementation(() => {
    const chunks = callCount === 0 ? INTERRUPT_CHUNKS : FINAL_CHUNKS;
    callCount++;
    let index = 0;
    return Promise.resolve({
      read: () => {
        if (index < chunks.length) return Promise.resolve({ done: false, value: chunks[index++] });
        return Promise.resolve({ done: true, value: undefined });
      },
      cancel: () => Promise.resolve(),
    });
  }),
}));

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "",
  fetchMeOrNull: vi.fn().mockResolvedValue({ user_id: "u1", username: "test", is_admin: false, has_api_key: true }),
  fetchInit: vi.fn().mockResolvedValue({
    workspaces: [{ workspace_id: "ws-1", title: "T", domain: "writing", workspace_path: "/t",
      created_at: "", updated_at: "", session_count: 1, active_style_id: null }],
    styles: [],
  }),
  fetchWorkspaceBootstrap: vi.fn().mockResolvedValue({
    threads: [{ thread_id: "thread-1", workspace_id: "ws-1", session_name: "测试会话", workspace_path: "/t",
      created_at: "", updated_at: "" }],
    outline: null, storyline: null, detail_outline: null, characters: null, novel: null, worldview: null,
  }),
  fetchThreadTraces: vi.fn().mockResolvedValue([]),
  fetchTraceDetail: vi.fn().mockResolvedValue(null),
  createThread: vi.fn().mockResolvedValue({ thread_id: "thread-1", workspace_id: "ws-1", session_name: "测试会话", workspace_path: "/t", created_at: "", updated_at: "" }),
  updateThread: vi.fn().mockResolvedValue({ thread_id: "thread-1" }),
  deleteThread: vi.fn().mockResolvedValue({}),
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
// InterviewOptions mock 为可交互占位：渲染选项按钮
vi.mock("@/components/workspace/InterviewOptions", () => ({
  InterviewOptions: ({ options, onSubmit }: { options: { label: string }[]; onSubmit: (t: string) => void }) => (
    <div data-testid="interview-options">
      {options.map((o, i) => (
        <button key={i} type="button" onClick={() => onSubmit(o.label)}>{o.label}</button>
      ))}
    </div>
  ),
}));
vi.mock("@/components/workspace/ImageReviewCard", () => ({ ImageReviewCard: () => null }));
vi.mock("@/components/workspace/SessionMenu", () => ({ SessionMenu: () => null }));

const { MemoryRouter } = await import("react-router-dom");
const { render } = await import("@testing-library/react");
const Home = (await import("@/pages/home")).default;

describe("T0b: HITL interrupt → resume", () => {
  beforeEach(async () => {
    callCount = 0;
    vi.clearAllMocks();
    const { resetStores } = await import("./helpers");
    await resetStores();
  });

  it("interrupt 后出现选项，用户选择后 resume，最终 completed", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/"]}><Home /></MemoryRouter>);

    // 第一次提交
    const input = await screen.findByRole("textbox", {}, { timeout: 10000 });
    await user.type(input, "写一个故事");
    await user.keyboard("{Enter}");

    // interrupt 后出现选项
    const options = await screen.findByTestId("interview-options", {}, { timeout: 10000 });
    expect(options).toBeInTheDocument();
    expect(screen.getByText("第 3 章主角是否遇到反派？")).toBeInTheDocument();
    expect(screen.getByText("遇到，加些冲突")).toBeInTheDocument();

    // 用户选择选项（触发 resume）
    await user.click(screen.getByText("遇到，加些冲突"));

    // resume 后最终 completed
    await waitFor(() => {
      expect(screen.getByText("正文完成")).toBeInTheDocument();
    }, { timeout: 10000 });

    const messages = document.querySelectorAll(".message.assistant");
    const lastAssistant = messages[messages.length - 1];
    expect(lastAssistant).toHaveAttribute("data-status", "completed");
  });

  it("第二次 streamRequest 调用是 resume 模式（body 含 resume 字段而非 prompt）", async () => {
    const { streamRequest } = await import("@/lib/stream");
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/"]}><Home /></MemoryRouter>);

    const input = await screen.findByRole("textbox", {}, { timeout: 10000 });
    await user.type(input, "写故事");
    await user.keyboard("{Enter}");

    await screen.findByTestId("interview-options", {}, { timeout: 10000 });
    await user.click(screen.getByText("遇到，加些冲突"));

    // 等第二次调用（resume）
    await waitFor(() => {
      expect(streamRequest).toHaveBeenCalledTimes(2);
    }, { timeout: 10000 });

    const secondCall = (streamRequest as ReturnType<typeof vi.fn>).mock.calls[1];
    expect(secondCall[0]).toBe("/api/screenplay/generate/stream");
    expect(secondCall[1].body).toHaveProperty("resume", "遇到，加些冲突");
    expect(secondCall[1].body).toHaveProperty("trace_id", "trace-1");
    // resume 模式不应有 prompt 字段
    expect(secondCall[1].body).not.toHaveProperty("prompt");
  });
});
