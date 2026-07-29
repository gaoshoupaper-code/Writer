// useTracePolling —— trace 详情轮询生命周期回归测试（RSK-003 / AC-002/003/004）。
//
// 守住 EVD-008/009/012 的核心契约：
//   - 同步失败保留旧快照但标陈旧、移除实时（不伪装成实时）。
//   - cancel_timeout 纳入终态集，不无限轮询。
//   - 单调 lifecycle_revision：旧响应不得覆盖新状态。
//   - 手动刷新立即去重同步，进行中防重复并发。
//   - 业务终态后继续观察到 trace 稳定（EDGE-009）。
//
// 时钟策略：初始同步用真实微任务（waitFor 落定），轮询推进用假时钟（advance）。

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useTracePolling } from "@/hooks/useTraceStream";
import type { TraceDetailLite } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  getTraceDetail: vi.fn(),
  refreshExecutorTrace: vi.fn(),
  getActiveSession: vi.fn(),
}));

import { getTraceDetail, refreshExecutorTrace, getActiveSession } from "@/lib/api";

const mockedGetTraceDetail = vi.mocked(getTraceDetail);
const mockedRefreshExecutorTrace = vi.mocked(refreshExecutorTrace);
const mockedGetActiveSession = vi.mocked(getActiveSession);

function makeDetail(overrides: Partial<TraceDetailLite["run"]> = {}): TraceDetailLite {
  return {
    run: {
      trace_id: "trace-1",
      workspace_id: "ws",
      thread_id: "t",
      session_name: "s",
      workspace_path: "/p",
      endpoint: "e",
      status: "running",
      started_at: "2026-07-29T00:00:00+00:00",
      path: "/p",
      service: "executor",
      event_count: 1,
      lifecycle_revision: 1,
      ...overrides,
    },
    nodes: [],
    todos: [],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedGetActiveSession.mockResolvedValue({ session_id: null } as any);
  // 全程假时钟：通过 runAllTicksAsync 冲刷微任务让初始 poll 落定。
  vi.useFakeTimers();
});

// 渲染后冲刷初始 poll 的微任务，让首帧同步落定。
// 只 yield 微任务，不推进 timer（避免触发下一轮 1s 轮询）。
async function settleInitial(result: { current: { detail: unknown } }) {
  for (let attempt = 0; attempt < 20 && result.current.detail == null; attempt++) {
    await act(async () => {
      // 直接调用的 async poll 需要微任务队列清空才落定。
      for (let i = 0; i < 20; i++) await Promise.resolve();
    });
  }
}

describe("useTracePolling 陈旧与失败语义 (FR-002/EDGE-003)", () => {
  it("首次成功后后续 refresh 失败：保留旧快照、标陈旧、移除实时", async () => {
    // 首 poll current=null → getTraceDetail；后续 running → refreshExecutorTrace。
    const first = makeDetail({ status: "running", lifecycle_revision: 5 });
    mockedGetTraceDetail.mockResolvedValueOnce(first);
    mockedRefreshExecutorTrace.mockRejectedValueOnce(new Error("网络断开"));

    const { result } = renderHook(() => useTracePolling("trace-1"));
    await settleInitial(result);
    expect(result.current.detail).not.toBeNull();
    expect(result.current.isStale).toBe(false);

    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });

    expect(result.current.detail).not.toBeNull();
    expect(result.current.isStale).toBe(true);
    expect(result.current.isLive).toBe(false);
    expect(result.current.error).toContain("网络断开");
  });
});

describe("useTracePolling 终态集 (EVD-009)", () => {
  it("cancel_timeout 进入终态：不继续高频轮询", async () => {
    mockedGetTraceDetail.mockResolvedValueOnce(makeDetail({ status: "running", lifecycle_revision: 1 }));
    // 二轮 running → refreshExecutorTrace，返回 cancel_timeout 终态。
    mockedRefreshExecutorTrace.mockResolvedValueOnce(
      makeDetail({ status: "cancel_timeout", lifecycle_revision: 2, trace_phase: "sealed", integrity_status: "incomplete" }),
    );

    const { result } = renderHook(() => useTracePolling("trace-1"));
    await settleInitial(result);
    expect(result.current.detail?.run.status).toBe("running");

    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });

    expect(result.current.detail?.run.status).toBe("cancel_timeout");
    expect(result.current.isLive).toBe(false);
  });
});

describe("useTracePolling 单调 revision (CON-006)", () => {
  it("旧 revision 响应不得覆盖新状态", async () => {
    const newer = makeDetail({ status: "running", lifecycle_revision: 10, event_count: 50 });
    const older = makeDetail({ status: "completed", lifecycle_revision: 5, event_count: 10 });
    mockedGetTraceDetail.mockResolvedValueOnce(newer);
    mockedRefreshExecutorTrace.mockResolvedValueOnce(older);

    const { result } = renderHook(() => useTracePolling("trace-1"));
    await settleInitial(result);
    expect(result.current.detail?.run.event_count).toBe(50);

    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });

    expect(result.current.detail?.run.event_count).toBe(50);
    expect(result.current.detail?.run.status).toBe("running");
  });
});

describe("useTracePolling 手动刷新 (DEC-004/AC-004)", () => {
  it("refresh() 立即去重同步，成功后展示最新版本", async () => {
    const first = makeDetail({ status: "running", lifecycle_revision: 1, event_count: 5 });
    const latest = makeDetail({ status: "running", lifecycle_revision: 2, event_count: 20 });
    mockedGetTraceDetail.mockResolvedValueOnce(first);

    const { result } = renderHook(() => useTracePolling("trace-1"));
    await settleInitial(result);
    expect(result.current.detail?.run.event_count).toBe(5);

    // 手动刷新：此时 detail 已存在 + running + service=executor → refreshExecutorTrace。
    mockedRefreshExecutorTrace.mockResolvedValueOnce(latest);
    await act(async () => {
      result.current.refresh();
      for (let i = 0; i < 20; i++) await Promise.resolve();
    });

    expect(result.current.detail?.run.event_count).toBe(20);
    expect(result.current.detail?.run.lifecycle_revision).toBe(2);
  });

  it("进行中的 refresh 禁止重复并发（AC-004：同时同类刷新最多 1 个）", async () => {
    const first = makeDetail({ status: "running", lifecycle_revision: 1 });
    mockedGetTraceDetail.mockResolvedValueOnce(first);

    const { result } = renderHook(() => useTracePolling("trace-1"));
    await settleInitial(result);

    const initialCalls = mockedRefreshExecutorTrace.mock.calls.length;
    let _resolveSlow: (v: TraceDetailLite) => void = () => {};
    mockedRefreshExecutorTrace.mockImplementationOnce(
      () => new Promise((res) => { _resolveSlow = res as any; }),
    );

    await act(async () => {
      result.current.refresh();
      result.current.refresh();
      result.current.refresh();
      for (let i = 0; i < 20; i++) await Promise.resolve();
    });

    expect(mockedRefreshExecutorTrace.mock.calls.length).toBe(initialCalls + 1);

    await act(async () => { _resolveSlow(first); });
  });
});

describe("useTracePolling 继续收敛 (EDGE-009)", () => {
  it("业务终态但 trace 未 sealed：降频继续观察，不停在缺 manifest", async () => {
    mockedGetTraceDetail.mockResolvedValueOnce(makeDetail({ status: "running", lifecycle_revision: 1 }));
    mockedRefreshExecutorTrace.mockResolvedValueOnce(
      makeDetail({ status: "completed", lifecycle_revision: 4, trace_phase: "sealing", integrity_status: "pending" }),
    );

    const { result } = renderHook(() => useTracePolling("trace-1"));
    await settleInitial(result);
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });

    expect(result.current.detail?.run.status).toBe("completed");
    expect(result.current.isLive).toBe(false);

    mockedGetTraceDetail.mockResolvedValueOnce(
      makeDetail({ status: "completed", lifecycle_revision: 5, trace_phase: "sealed", integrity_status: "verified" }),
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(3500); });

    expect(result.current.detail?.run.trace_phase).toBe("sealed");
  });
});
