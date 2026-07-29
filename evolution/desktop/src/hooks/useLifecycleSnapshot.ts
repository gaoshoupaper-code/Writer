// useLifecycleSnapshot —— 统一生命周期观察 hook（FR-007, DEC-007, CON-006, EVD-015）。
//
// 解决桌面端"全局割裂、延迟显示、不一致"的根因（EVD-015）：
//   - 各页面原各自 1/2/5/10s 独立轮询 + 本地 statusLabel 映射 + 不同停止条件，
//     导致同对象在不同页面显示互斥状态、刷新后反转。
//   - 本 hook 提供单一权威观察契约：消费后端 lifecycle_revision，拒绝旧 revision
//     覆盖新状态（CON-006）；停止后持续观察到 trace sealed + integrity 终态；
//     稳定终态后退避降频（NFR-004 有界）；失败保留最后快照 + 陈旧标识。
//
// 设计要点（DEC-007 四个时延阈值）：
//   - 持续观察直到四维全部稳定（business 终态 + trace sealed + integrity 终态）。
//   - 稳定后不再高频轮询（NFR-004：不堆积并发请求、不随时间增长）。
//   - 单 revision 单调：旧响应晚到不得覆盖新状态（CON-006 防旧快照）。

import { useEffect, useRef, useState } from "react";

/** 四维全部稳定的判定：business 终态 + trace 已封存 + integrity 已判定。 */
export function isLifecycleFullySettled(snapshot: LifecycleSnapshot | null): boolean {
  if (!snapshot) return false;
  const businessTerminal = TERMINAL_BUSINESS_STATUSES.has(snapshot.status);
  // trace_phase sealed/degraded 表示封存完成；recording/sealing 仍在封存中（EDGE-009）。
  const traceSealed = snapshot.trace_phase == null
    || snapshot.trace_phase === "sealed"
    || snapshot.trace_phase === "degraded";
  // integrity 终态：sealed 后的 verified/incomplete/conflict/legacy；pending 仍在校验。
  const integrityTerminal = snapshot.integrity_status !== "pending" && snapshot.integrity_status !== undefined;
  return businessTerminal && traceSealed && integrityTerminal;
}

const TERMINAL_BUSINESS_STATUSES = new Set([
  "completed", "failed", "cancelled", "cancel_timeout", "interrupted",
]);

/** 统一对外状态字典（FR-007/CON-009）：所有页面用同一文案，不分叉。 */
export function lifecycleStatusLabel(status: string | undefined): string {
  switch (status) {
    case "pending": return "排队中";
    case "running": return "运行中";
    case "awaiting_input": return "等待输入";
    case "cancelling": return "取消中";
    case "completed": return "已完成";
    case "failed": return "失败";
    case "cancelled": return "已取消";
    case "cancel_timeout": return "取消超时";
    case "interrupted": return "已中断";
    default: return status || "未知";
  }
}

export type LifecycleSnapshot = {
  status: string;
  integrity_status?: string;
  trace_phase?: string | null;
  lifecycle_revision?: number;
  /** 业务对象的可选附加字段（各页面自行透传，hook 不解释）。 */
  [key: string]: unknown;
};

export type LifecycleSnapshotState<T extends LifecycleSnapshot> = {
  /** 当前权威快照（已通过 revision 单调过滤）。 */
  snapshot: T | null;
  /** 是否仍在主动观察（未完全稳定）。 */
  isLive: boolean;
  /** 数据陈旧：最近一次拉取失败，显示的是最后成功快照（CON-006 失败语义）。 */
  isStale: boolean;
  /** 拉取错误信息（用于显示故障态，不清空成"无活跃"）。 */
  error: string | null;
  /** 首次加载中。 */
  loading: boolean;
};

/**
 * 统一生命周期观察 hook。
 *
 * @param objectId 观察对象 ID（用于日志/去重，非请求参数）。
 * @param fetcher 拉取权威快照的函数（返回 null 表示对象不存在）。
 * @param options.intervalMs 活跃态轮询间隔（默认 1s，满足 NFR-002 跨页 P100≤3s）。
 * @param options.settledIntervalMs 完全稳定后的降频间隔（默认 30s，NFR-004 有界）。
 * @param options.errorBackoffMs 失败退避（默认 3s，不随时间增长）。
 */
export function useLifecycleSnapshot<T extends LifecycleSnapshot>(
  objectId: string | null,
  fetcher: () => Promise<T | null>,
  options: {
    intervalMs?: number;
    settledIntervalMs?: number;
    errorBackoffMs?: number;
  } = {},
): LifecycleSnapshotState<T> {
  const intervalMs = options.intervalMs ?? 1000;
  const settledIntervalMs = options.settledIntervalMs ?? 30_000;
  const errorBackoffMs = options.errorBackoffMs ?? 3000;

  const [snapshot, setSnapshot] = useState<T | null>(null);
  const [isLive, setIsLive] = useState(false);
  const [isStale, setIsStale] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // 已见最大 revision（CON-006：拒绝旧 revision 覆盖新状态）。
  const lastRevisionRef = useRef<number>(-1);
  // 防并发：同一对象不积累并发中的同类请求（NFR-004 单请求约束）。
  const inFlightRef = useRef(false);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    if (!objectId) {
      setSnapshot(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    lastRevisionRef.current = -1;

    const tick = async () => {
      if (cancelled || inFlightRef.current) return;
      inFlightRef.current = true;
      try {
        const fetched = await fetcherRef.current();
        if (cancelled || fetched == null) return;
        const revision = fetched.lifecycle_revision ?? 0;
        // CON-006 单调：旧 revision 不得覆盖新状态（乱序响应防护）。
        if (revision < lastRevisionRef.current) return;
        lastRevisionRef.current = revision;
        setSnapshot(fetched);
        setIsStale(false);
        setError(null);
        setLoading(false);
      } catch (e) {
        if (cancelled) return;
        // 失败保留最后成功快照，标陈旧（不清空成"无活跃"）。
        setIsStale(true);
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      } finally {
        inFlightRef.current = false;
      }
    };

    const scheduleNext = () => {
      if (cancelled) return;
      // 完全稳定后降频（NFR-004：稳定终态后不维持原高频）。
      const settled = isLifecycleFullySettled(snapshot);
      setIsLive(!settled);
      const delay = settled ? settledIntervalMs : (isStale ? errorBackoffMs : intervalMs);
      timer = setTimeout(async () => {
        await tick();
        scheduleNext();
      }, delay);
    };

    tick().then(scheduleNext);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // 依赖 objectId + snapshot 的稳定性切换（isStale 影响退避）；不依赖 fetcher 引用（用 ref）。
  }, [objectId, snapshot?.status, snapshot?.trace_phase, snapshot?.integrity_status, isStale, intervalMs, settledIntervalMs, errorBackoffMs]);

  return { snapshot, isLive, isStale, error, loading };
}
