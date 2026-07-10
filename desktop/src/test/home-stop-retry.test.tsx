/**
 * T0c 集成测试：stop → retry
 *
 * 验证路径：
 *   1. 用户发送消息 → SSE 进行中 → 点击"停止"按钮 → reader.cancel()
 *      → SSE 循环退出 → loading 恢复 false
 *   2.（独立场景）SSE 失败 → message status: failed → 出现"重试"按钮
 *      → 点击重试 → 重新 performSubmit → completed
 *
 * 注意：当前代码里 cancel 后 SSE 循环正常退出（read 返回 done:true），
 * 不会走 AbortError 分支，所以 message status 不会被标 stopped。
 * 这是当前真实行为——P0 测试以此为准。P1 重构时可修正为 stopped。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";

const encoder = new TextEncoder();
function sseEvent(type: string, data: unknown): string {
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

// 场景控制：第几次调用返回什么流
// "stop" = 只发一个 chunk 然后阻塞等 cancel
// "error" = 直接抛错（触发 failed）
// "final" = 完整流到 final
let mode: "stop" | "error" | "final" = "stop";

const STOP_CHUNKS: Uint8Array[] = [
  encoder.encode(sseEvent("trace_event", {
    trace_id: "trace-1", event_id: "evt-1", sequence: 1, type: "run_start", status: "running",
    timestamp: "2026-01-01T00:00:00Z", source: "system",
    input: { workspace_id: "ws-1", thread_id: "thread-1", session_name: "测试会话", endpoint: "x" },
  })),
  encoder.encode(sseEvent("model_stream", { content: "写到一半的内容" })),
];

const FINAL_CHUNKS: Uint8Array[] = [
  encoder.encode(sseEvent("model_stream", { content: "重试后的完整正文" })),
  encoder.encode(sseEvent("final", {
    mode: "screenplay", thread_id: "thread-1", workspace_id: "ws-1", session_name: "测试会话",
    workspace_path: "/test", title: "T", content: "重试后的完整正文", logline: "", synopsis: "",
    beats: [], markdown: "重试后的完整正文", evaluation_markdown: "",
  })),
];

vi.mock("@/lib/stream", () => ({
  streamRequest: vi.fn().mockImplementation(() => {
    const currentMode = mode;
    let index = 0;
    let cancelled = false;
    let pendingResolve: ((v: { done: boolean; value: Uint8Array | undefined }) => void) | null = null;

    if (currentMode === "error") {
      // 直接 reject（模拟连接失败）
      return Promise.reject(new Error("connection refused"));
    }

    const chunks = currentMode === "stop" ? STOP_CHUNKS : FINAL_CHUNKS;
    return Promise.resolve({
      read: () => {
        if (cancelled) return Promise.resolve({ done: true, value: undefined });
        if (index < chunks.length) {
          return Promise.resolve({ done: false, value: chunks[index++] });
        }
        if (currentMode === "stop") {
          // 阻塞等 cancel
          return new Promise<{ done: boolean; value: Uint8Array | undefined }>((resolve) => {
            pendingResolve = resolve;
          });
        }
        return Promise.resolve({ done: true, value: undefined });
      },
      cancel: () => {
        cancelled = true;
        if (pendingResolve) {
          pendingResolve({ done: true, value: undefined });
          pendingResolve = null;
        }
        return Promise.resolve();
      },
    });
  }),
}));

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "",
  apiFetch: vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve("") }),
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
vi.mock("@/components/workspace/InterviewOptions", () => ({ InterviewOptions: () => null }));
vi.mock("@/components/workspace/ImageReviewCard", () => ({ ImageReviewCard: () => null }));
vi.mock("@/components/workspace/SessionMenu", () => ({ SessionMenu: () => null }));

const { MemoryRouter } = await import("react-router-dom");
const { render } = await import("@testing-library/react");
const Home = (await import("@/pages/home")).default;

describe("T0c: stop → retry", () => {
  beforeEach(async () => {
    mode = "stop";
    vi.clearAllMocks();
    const { resetStores } = await import("./helpers");
    await resetStores();
  });

  it("生成中点击停止，loading 恢复 false，已生成内容保留", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/"]}><Home /></MemoryRouter>);

    const input = await screen.findByRole("textbox", {}, { timeout: 10000 });
    await user.type(input, "写个故事");
    await user.keyboard("{Enter}");

    // 等"停止"按钮出现（说明 loading=true）
    const stopButton = await screen.findByRole("button", { name: "停止" }, { timeout: 10000 });
    await user.click(stopButton);

    // 停止后 loading 恢复：停止/生成中按钮消失，发送按钮恢复
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "停止" })).not.toBeInTheDocument();
    }, { timeout: 10000 });

    // 已生成的部分内容应保留
    await waitFor(() => {
      expect(screen.getByText("写到一半的内容")).toBeInTheDocument();
    }, { timeout: 5000 });
  });

  it("生成失败后出现重试按钮，点击重试重新生成到 completed", async () => {
    // 第一次提交会失败（mode=error），重试会成功（mode=final）
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/"]}><Home /></MemoryRouter>);

    const input = await screen.findByRole("textbox", {}, { timeout: 10000 });

    // 第一次：error 模式
    mode = "error";
    await user.type(input, "测试prompt");
    await user.keyboard("{Enter}");

    // 等失败后出现重试按钮（FailedView 的"↻ 再试一次"）
    const retryButton = await screen.findByRole("button", { name: /再试一次/ }, { timeout: 10000 });

    // 验证 message 标记 failed
    const messages = document.querySelectorAll(".message.assistant");
    const last = messages[messages.length - 1];
    expect(last).toHaveAttribute("data-status", "failed");

    // 切换到 final 模式，点重试
    mode = "final";
    await user.click(retryButton);

    // retry 后最终 completed
    await waitFor(() => {
      expect(screen.getByText("重试后的完整正文")).toBeInTheDocument();
    }, { timeout: 10000 });

    const messages2 = document.querySelectorAll(".message.assistant");
    const last2 = messages2[messages2.length - 1];
    expect(last2).toHaveAttribute("data-status", "completed");
  });
});
