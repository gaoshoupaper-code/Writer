/**
 * traceStore —— Trace 列表 / 详情 / 历史回放（从 home.tsx 迁移）
 *
 * 职责：trace runs 列表加载、trace detail 加载/增量更新、历史回放、trace 删除。
 *
 * 被 executionStore 通过 ExecutionDeps 间接调用（setTraceRuns/setTraceDetail 等）。
 */
import { create } from "zustand";
import { toast } from "sonner";
import type { TraceDetail, TraceRunSummary } from "@/lib/types";
import { deleteTrace as deleteTraceRequest, fetchThreadTraces, fetchTraceDetail, stopScreenplay as stopScreenplayRequest } from "@/lib/api";

interface TraceState {
  traceRuns: TraceRunSummary[];
  activeTraceId: string;
  liveTraceId: string;
  traceDetail: TraceDetail | null;
  historyDetails: Map<string, TraceDetail>;
  traceLoading: boolean;
  deletingTraceId: string;
  stoppingTraceId: string;

  // actions
  loadTraceRuns: (threadId: string) => Promise<void>;
  loadTraceDetail: (threadId: string, traceId: string) => Promise<void>;
  loadHistoryDetails: (threadId: string, traceIds: string[]) => Promise<void>;
  deleteTrace: (threadId: string, traceId: string) => Promise<void>;
  stopTrace: (threadId: string, traceId: string) => Promise<void>;
  clearTrace: () => void;
  resetForThread: () => void;

  // executionStore 注入用的直接 setter（通过 deps 暴露）
  setTraceRuns: (updater: TraceRunSummary[] | ((current: TraceRunSummary[]) => TraceRunSummary[])) => void;
  setTraceDetail: (updater: TraceDetail | null | ((current: TraceDetail | null) => TraceDetail | null)) => void;
  setActiveTraceId: (id: string) => void;
  setLiveTraceId: (id: string) => void;
}

export const useTraceStore = create<TraceState>((set, get) => ({
  traceRuns: [],
  activeTraceId: "",
  liveTraceId: "",
  traceDetail: null,
  historyDetails: new Map(),
  traceLoading: false,
  deletingTraceId: "",
  stoppingTraceId: "",

  loadTraceRuns: async (threadId) => {
    if (!threadId) {
      set({ traceRuns: [], activeTraceId: "", liveTraceId: "", traceDetail: null });
      return;
    }
    set({ traceLoading: true });
    try {
      const data = await fetchThreadTraces(threadId);
      set((state) => ({
        traceRuns: data,
        traceDetail: null,
        activeTraceId: data.some((run) => run.trace_id === state.activeTraceId) ? state.activeTraceId : data[0]?.trace_id || "",
      }));
    } catch (traceError) {
      set({ traceRuns: [], activeTraceId: "", traceDetail: null });
      toast.error(traceError instanceof Error ? traceError.message : "无法加载 Trace 列表。");
    } finally {
      set({ traceLoading: false });
    }
  },

  loadTraceDetail: async (threadId, traceId) => {
    if (!threadId || !traceId) {
      set({ traceDetail: null });
      return;
    }
    // live trace 不重新加载
    if (get().liveTraceId === traceId) return;

    set({ traceLoading: true });
    try {
      const detail = await fetchTraceDetail(threadId, traceId);
      set({ traceDetail: detail });
    } catch (traceError) {
      set({ traceDetail: null });
      toast.error(traceError instanceof Error ? traceError.message : "无法加载 Trace 详情。");
    } finally {
      set({ traceLoading: false });
    }
  },

  loadHistoryDetails: async (threadId, traceIds) => {
    if (!threadId || traceIds.length === 0) {
      set({ historyDetails: new Map() });
      return;
    }
    const map = new Map<string, TraceDetail>();
    for (const id of traceIds) {
      try {
        map.set(id, await fetchTraceDetail(threadId, id));
      } catch {
        // 单个 trace 加载失败不阻塞其他
      }
    }
    set({ historyDetails: map });
  },

  deleteTrace: async (threadId, traceId) => {
    if (!threadId || !traceId || get().deletingTraceId) return;
    const run = get().traceRuns.find((item) => item.trace_id === traceId);
    if (run?.status === "running") {
      toast.error("运行中的 Trace 不能删除。");
      return;
    }
    set({ deletingTraceId: traceId });
    try {
      await deleteTraceRequest(threadId, traceId);
      set((state) => {
        const next = state.traceRuns.filter((item) => item.trace_id !== traceId);
        const patch: Partial<TraceState> = { traceRuns: next };
        if (state.activeTraceId === traceId) {
          patch.traceDetail = null;
          patch.activeTraceId = next[0]?.trace_id || "";
        }
        if (state.liveTraceId === traceId) {
          patch.liveTraceId = "";
        }
        return patch as TraceState;
      });
    } catch (deleteError) {
      const message = deleteError instanceof Error ? deleteError.message : "";
      toast.error(message.includes("409") ? "运行中的 Trace 不能删除。" : "无法删除 Trace。");
    } finally {
      set({ deletingTraceId: "" });
    }
  },

  stopTrace: async (threadId, traceId) => {
    // D-停止真生效：调 POST /stop（后端 request_user_stop + cancel_run_task 真终止）。
    // 停的是当前 liveTraceId 时，同步触发 executionStore.stop() 关闭前端 SSE，
    // 走常规 client 收尾避免 SSE 继续推已作废事件；否则只靠后端 task.cancel。
    if (!threadId || !traceId || get().stoppingTraceId) return;
    const run = get().traceRuns.find((item) => item.trace_id === traceId);
    if (!run || run.status !== "running") {
      toast.error("仅运行中的 Trace 可停止。");
      return;
    }
    set({ stoppingTraceId: traceId });
    try {
      await stopScreenplayRequest(threadId, traceId);
      if (get().liveTraceId === traceId) {
        // 延迟 import 避免循环依赖（execution.ts 通过 deps 反向引用 trace store）
        const { useExecutionStore } = await import("./execution");
        useExecutionStore.getState().stop();
      }
      // 刷新 trace 列表让状态从 running 变 cancelled
      await get().loadTraceRuns(threadId);
      toast.success("已停止运行中的 Trace。");
    } catch (stopError) {
      const message = stopError instanceof Error ? stopError.message : "";
      toast.error(message || "停止 Trace 失败。");
    } finally {
      set({ stoppingTraceId: "" });
    }
  },

  clearTrace: () => set({ traceRuns: [], activeTraceId: "", liveTraceId: "", traceDetail: null }),
  resetForThread: () => set({ traceRuns: [], activeTraceId: "", liveTraceId: "", traceDetail: null }),

  setTraceRuns: (updater) =>
    set((state) => ({
      traceRuns: typeof updater === "function" ? (updater as (c: TraceRunSummary[]) => TraceRunSummary[])(state.traceRuns) : updater,
    })),
  setTraceDetail: (updater) =>
    set((state) => ({
      traceDetail: typeof updater === "function" ? (updater as (c: TraceDetail | null) => TraceDetail | null)(state.traceDetail) : updater,
    })),
  setActiveTraceId: (id) => set({ activeTraceId: id }),
  setLiveTraceId: (id) => set({ liveTraceId: id }),
}));
