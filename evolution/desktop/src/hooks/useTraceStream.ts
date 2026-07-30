// useTracePolling —— trace 详情轮询生命周期 hook（trace 实时可见性根治，FR-001/002/003）。
//
// 根治 EVD-008/009/012 的"页面静默停在旧快照、错误伪装成实时、终态后停止收敛"：
//   - 陈旧语义（FR-002/EDGE-003）：后续 refresh 失败保留最后可信快照，但立即移除实时
//     标记、设 isStale、记录最后成功同步时间，不让"实时旧快照"伪装成实时。
//   - 单调 revision（CON-006）：拒绝旧 lifecycle_revision 覆盖新状态（乱序响应防护）。
//   - 统一终态集（EVD-009）：cancel_timeout 纳入终态集合，不再无限轮询。
//   - 业务终态后继续收敛（EVD-012/EDGE-009）：业务终态≠Trace 稳定；继续观察到
//     trace_phase sealed + integrity 终态，再降频，不停在缺 manifest/旧完整性结论。
//   - 手动刷新（DEC-004）：暴露 refresh()，不依赖下一轮自动定时器，立即去重同步。
//   - active-session 解耦（NFR-003/EVD-011）：辅助查询不阻塞 Trace 数据同步。
//
// 设计变更（设计 20260720_203000）：从 SSE 主导改为 Pull 主导。
//   Pull：1s 轮询拿全量 detail（后端投影的 nodes）→ running 持续轮询；
//   终态停止高频；error 退避但标陈旧；手动刷新立即拉取。

import { useCallback, useEffect, useRef, useState } from "react";
import {
  getTraceDetail,
  refreshExecutorTrace,
  getActiveSession,
  type ActiveSession,
} from "@/lib/api";
import type { TraceDetailLite } from "@/lib/types";

/** 业务终态集合：进入这些状态后停止高频轮询（cancel_timeout 纳入，EVD-009 修复）。 */
const TERMINAL_STATUSES = new Set([
  "completed", "failed", "cancelled", "cancel_timeout", "interrupted",
]);

/** running 状态下的轮询间隔（NFR-001：新事件 P100≤3s 可见）。 */
const POLL_INTERVAL_MS = 1000;
/** 业务终态但 Trace 仍封存中的降频间隔（EDGE-009：继续观察到稳定）。 */
const CONVERGE_INTERVAL_MS = 3000;
/** 收敛尝试上限：超过后从 CONVERGE 降到 SETTLED 频率（NFR-003：终态后不无限高频）。 */
const MAX_CONVERGE_ATTEMPTS = 10;
/** 完全稳定后的降频间隔（NFR-003：不维持原高频）。 */
const SETTLED_INTERVAL_MS = 30000;
/** 连续 error 后的退避间隔（不狂打失败请求）。 */
const ERROR_BACKOFF_MS = 3000;

/** 四维全部稳定的判定（EDGE-009）：业务终态 + trace 封存 + integrity 终态。 */
function isFullySettled(detail: TraceDetailLite | null): boolean {
  if (!detail) return false;
  const run = detail.run;
  const businessTerminal = TERMINAL_STATUSES.has(run.status);
  // trace_phase sealed/degraded 表示封存完成；recording/sealing 仍在封存中。
  const traceSealed = run.trace_phase == null
    || run.trace_phase === "sealed"
    || run.trace_phase === "degraded";
  // integrity 终态：sealed 后的 verified/incomplete/conflict/legacy；pending 仍在校验。
  const integrityTerminal = run.integrity_status !== "pending"
    && run.integrity_status !== undefined;
  return businessTerminal && traceSealed && integrityTerminal;
}

type TracePollingState = {
  detail: TraceDetailLite | null;
  /** 是否仍高频轮询（running 状态 true；终态/陈旧 false）。 */
  isLive: boolean;
  /** 数据陈旧：最近一次 refresh 失败，显示最后可信快照（FR-002 失败语义）。 */
  isStale: boolean;
  /** 最后一次成功同步的时间戳（FR-001/DEC-004 数据新鲜度）。 */
  lastSyncedAt: number | null;
  /** 手动刷新立即去重同步（DEC-004）。返回是否发起新请求。 */
  refresh: () => void;
  /** 手动刷新进行中（防重复并发，AC-004）。 */
  refreshing: boolean;
  /** 当前活跃 session（running 时低频反查，供停止按钮用）。null = 无活跃 session。 */
  activeSession: ActiveSession | null;
  loading: boolean;
  error: string | null;
  /**
   * FR-003：trace 详情的可用性语义（与列表 trace_availability 对齐）。
   * - available：详情已加载，可正常展示。
   * - preparing：已回填 trace_id 但 executor 尚未可读，吸收跨进程/摄入竞态窗口。
   * - unavailable：终态且权威来源确定不可读，显示"Trace 不可用，可重跑"。
   * - loading：首次加载中，尚无结论。
   */
  availability: "available" | "preparing" | "unavailable" | "loading";
};

export function useTracePolling(traceId: string | null): TracePollingState {
  const [detail, setDetail] = useState<TraceDetailLite | null>(null);
  const [isLive, setIsLive] = useState(false);
  const [isStale, setIsStale] = useState(false);
  const [lastSyncedAt, setLastSyncedAt] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [activeSession, setActiveSession] = useState<ActiveSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [availability, setAvailability] = useState<
    "available" | "preparing" | "unavailable" | "loading"
  >("loading");

  // refs 避免闭包旧值。
  const detailRef = useRef<TraceDetailLite | null>(null);
  const lastRevisionRef = useRef<number>(-1);
  // 单飞：同一 trace 同类数据同步在任意时刻最多 1 个在途（NFR-003）。
  const inFlightRef = useRef(false);
  // 手动刷新触发的 promise resolver 队列（让 refresh() 反映真实发起）。
  const manualTriggerRef = useRef<(() => void) | null>(null);

  // 成功路径：单调 revision 过滤 + 状态/陈旧/同步时间更新。
  const applySuccess = useCallback((next: TraceDetailLite) => {
    const revision = next.run.lifecycle_revision ?? 0;
    // CON-006 单调：旧 revision 不得覆盖新状态（乱序响应防护）。
    if (revision < lastRevisionRef.current) return false;
    lastRevisionRef.current = revision;
    setDetail(next);
    detailRef.current = next;
    setError(null);
    setIsStale(false);
    setLastSyncedAt(Date.now());
    setLoading(false);
    setAvailability("available");
    return true;
  }, []);

  // 一次数据同步（refresh 或 detail 读取），返回是否成功。
  const syncOnce = useCallback(async (id: string): Promise<boolean> => {
    if (inFlightRef.current) return false;
    inFlightRef.current = true;
    try {
      const current = detailRef.current;
      let next: TraceDetailLite;
      if (current?.run.service === "executor" && !TERMINAL_STATUSES.has(current.run.status)) {
        next = await refreshExecutorTrace(id);
      } else {
        try {
          next = await getTraceDetail(id);
        } catch (loadError) {
          // 单次测试先拿到 trace_id、后完成首次摄入。首个 404 立即按需同步，
          // 避免把正常的跨服务时序窗口呈现成"Trace not found"。
          if (current !== null) throw loadError;
          next = await refreshExecutorTrace(id);
        }
      }
      return applySuccess(next);
    } finally {
      inFlightRef.current = false;
    }
  }, [applySuccess]);

  // active-session 反查独立于数据同步（NFR-003：辅助查询不阻塞 Trace 刷新）。
  const syncActiveSession = useCallback(async (id: string) => {
    try {
      const session = await getActiveSession(id);
      setActiveSession(session);
    } catch {
      // 反查失败不阻断——停止按钮可能不出现，但其它功能正常。
      setActiveSession(null);
    }
  }, []);

  useEffect(() => {
    if (!traceId) {
      setDetail(null);
      detailRef.current = null;
      lastRevisionRef.current = -1;
      setLoading(false);
      setIsLive(false);
      setIsStale(false);
      setLastSyncedAt(null);
      setAvailability("loading");
      return;
    }

    let ignore = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let sessionTimer: ReturnType<typeof setTimeout> | null = null;
    // 收敛计数：业务终态后继续观察封存的尝试次数；超阈值后降频，避免无限高频（NFR-003）。
    let convergeAttempts = 0;
    lastRevisionRef.current = -1;

    const stop = () => {
      ignore = true;
      if (timer) { clearTimeout(timer); timer = null; }
      if (sessionTimer) { clearTimeout(sessionTimer); sessionTimer = null; }
    };

    // 数据同步轮询：依据当前状态选择间隔与停止策略。
    const poll = async () => {
      try {
        const ok = await syncOnce(traceId);
        if (ignore) return;
        if (!ok) {
          // syncOnce 返回 false 仅因单飞占用或旧 revision 被拒——都不是停止条件，
          // 必须重新安排下一轮，否则自动轮询会永久死掉（AC-003/FR-002）。
          timer = setTimeout(poll, POLL_INTERVAL_MS);
          return;
        }
        const current = detailRef.current;
        const status = current?.run.status;

        if (current && isFullySettled(current)) {
          // 四维全稳定：停止主动轮询（用户可手动刷新）。
          setIsLive(false);
          setActiveSession(null);
          return;
        }
        // 业务终态但 Trace 仍封存中：降频继续收敛（EDGE-009），不立刻停。
        // 但有界：收敛超过阈值后升级到 SETTLED_INTERVAL_MS，避免无限高频（NFR-003）。
        if (current && status && TERMINAL_STATUSES.has(status)) {
          setIsLive(false);
          setActiveSession(null);
          convergeAttempts += 1;
          const delay = convergeAttempts > MAX_CONVERGE_ATTEMPTS
            ? SETTLED_INTERVAL_MS
            : CONVERGE_INTERVAL_MS;
          timer = setTimeout(poll, delay);
          return;
        }
        // 运行中：标记 live + 降频到 POLL_INTERVAL_MS。
        setIsLive(true);
        convergeAttempts = 0;
        timer = setTimeout(poll, POLL_INTERVAL_MS);
      } catch (err) {
        if (ignore) return;
        const msg = err instanceof Error ? err.message : "trace 查询失败";
        if (detailRef.current === null) {
          // 首次加载失败：区分 preparing / unavailable / 原始错误。
          if (msg.includes("尚未可从 executor 读取")) {
            setAvailability("preparing");
            setError(null);
          } else if (msg.includes("同步后仍不可用")) {
            setAvailability("unavailable");
            setError(null);
          } else {
            setError(msg);
          }
          setLoading(false);
        } else {
          // 后续失败：保留旧快照但立即移除实时标记、标陈旧（EVD-009 修复）。
          setIsLive(false);
          setIsStale(true);
          setError(msg);
        }
        // 退避后继续轮询（可能是临时网络问题，下个 tick 恢复）。
        timer = setTimeout(poll, ERROR_BACKOFF_MS);
      }
    };

    // active-session 独立低频轮询：不阻塞数据同步（NFR-003）。
    // 自重排：即便首帧 detail 还没落定也要持续重排，直到拿到运行态 detail 或终态停止。
    const pollSession = async () => {
      if (ignore) return;
      const current = detailRef.current;
      // 仅运行态反查（终态无活跃 session）。
      if (current && !TERMINAL_STATUSES.has(current.run.status)) {
        await syncActiveSession(traceId);
        if (!ignore) sessionTimer = setTimeout(pollSession, POLL_INTERVAL_MS);
      } else if (current && TERMINAL_STATUSES.has(current.run.status)) {
        // 终态：停止 session 反查。
        return;
      } else {
        // detail 尚未就绪：稍后重试（避免首帧 null 导致永久停止反查）。
        if (!ignore) sessionTimer = setTimeout(pollSession, POLL_INTERVAL_MS);
      }
    };

    // 手动刷新触发器（DEC-004）：refresh() 调用时不等下一轮定时器。
    manualTriggerRef.current = () => {
      if (ignore || inFlightRef.current) return;
      setRefreshing(true);
      // 数据同步独立发起；不干扰定时轮询节拍。
      syncOnce(traceId).then((ok) => {
        if (!ignore && ok) {
          // 成功后若仍在运行态，顺手刷新 active-session。
          const current = detailRef.current;
          if (current && !TERMINAL_STATUSES.has(current.run.status)) {
            void syncActiveSession(traceId);
          }
        }
      }).catch(() => {
        // 手动刷新失败：保留旧快照 + 标陈旧（AC-004 失败语义）。
        if (!ignore && detailRef.current) {
          setIsStale(true);
        }
      }).finally(() => {
        if (!ignore) setRefreshing(false);
      });
    };

    // 首屏立即拉一次，然后由 poll 自己安排后续。
    setLoading(true);
    setError(null);
    setDetail(null);
    detailRef.current = null;
    setAvailability("loading");
    poll();
    pollSession();

    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [traceId]);

  const refresh = useCallback(() => {
    manualTriggerRef.current?.();
  }, []);

  return {
    detail, isLive, isStale, lastSyncedAt, refresh, refreshing,
    activeSession, loading, error, availability,
  };
}

// 向后兼容别名：旧调用方 useTraceStream(...) 仍可用，等价于 useTracePolling。
export const useTraceStream = useTracePolling;
