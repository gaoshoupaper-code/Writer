// useTracePolling —— trace 详情轮询生命周期 hook（trace 稳定性重构）。
//
// 设计变更（设计 20260720_203000）：从 SSE 主导改为 Pull 主导。
//   旧版（SSE）：getTraceDetail 打底 → 探活 → 开 evoTraceStream 收 node patch
//                → 断了永久停在旧值，无重连，event_count 不更新
//   新版（Pull）：1s 轮询 /traces/{id} 拿全量 detail（含后端投影的 nodes）
//                → running 持续轮询；终态停止；error 退避下个 tick 恢复
//
// 为什么 Pull 比 SSE 稳（根因 B/C 的解法）：
//   - 幂等：每次轮询都是完整快照，断了下个 tick 自动恢复
//   - event_count 实时：detail.run.event_count 每次轮询都从 DB 读，不再卡住
//   - 状态准：runs.status 是后端 recorder 心跳刷新的单一真相源，前端直接读
//   - 零状态协调：前端不需要 SSE hub / queue / source 判定，逻辑极简
//
// 为什么不需要 events/since 接口：
//   详情页主视图只需 run + nodes（后端投影），events 只给抽屉懒加载用
//   （点开 LLM 节点看 input/output 时走 /events?event_ids= 老接口）。
//   投影完全由后端 /traces/{id} 负责，前端零投影逻辑。

import { useEffect, useRef, useState } from "react";
import { getTraceDetail, getActiveSession, type ActiveSession } from "@/lib/api";
import type { TraceDetailLite } from "@/lib/types";

/** 终态集合：进入这些状态后停止轮询。 */
const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled", "interrupted"]);

/** running 状态下的轮询间隔（设计 A5：1s）。 */
const POLL_INTERVAL_MS = 1000;
/** 连续 error 后的退避间隔（避免狂打失败请求）。 */
const ERROR_BACKOFF_MS = 3000;

type TracePollingState = {
  detail: TraceDetailLite | null;
  /** 是否在轮询（running 状态下 true，终态 false）。 */
  isLive: boolean;
  /** 当前活跃 session（running 时每次轮询顺便反查，供停止按钮使用）。null = 无活跃 session。 */
  activeSession: ActiveSession | null;
  loading: boolean;
  error: string | null;
};

export function useTracePolling(traceId: string | null): TracePollingState {
  const [detail, setDetail] = useState<TraceDetailLite | null>(null);
  const [isLive, setIsLive] = useState(false);
  const [activeSession, setActiveSession] = useState<ActiveSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 保持最新 detail 的引用，避免闭包旧值（轮询回调读最新状态判断是否继续）。
  const detailRef = useRef<TraceDetailLite | null>(null);

  useEffect(() => {
    if (!traceId) {
      setDetail(null);
      detailRef.current = null;
      setLoading(false);
      setIsLive(false);
      return;
    }

    let ignore = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const stop = () => {
      ignore = true;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    };

    const poll = async () => {
      try {
        const next = await getTraceDetail(traceId);
        if (ignore) return;

        setDetail(next);
        detailRef.current = next;
        setError(null);
        setLoading(false);

        const status = next.run.status;
        if (TERMINAL_STATUSES.has(status)) {
          // 终态：停止轮询 + 清掉活跃 session（终态没有可停的 session）。
          setIsLive(false);
          setActiveSession(null);
          return;
        }
        // 非终态（running / awaiting_input）：标记 live + 顺便反查活跃 session。
        // 每次轮询都反查——self_trace_id 写入有延迟（create_run 后才 UPDATE），
        // 只查一次可能拿到 null，按钮永远不出现。轮询式反查保证写入后立即可见。
        setIsLive(true);
        try {
          const session = await getActiveSession(traceId);
          if (!ignore) setActiveSession(session);
        } catch {
          // 反查失败不阻断——停止按钮可能不出现，但其它功能正常。
          if (!ignore) setActiveSession(null);
        }
        timer = setTimeout(poll, POLL_INTERVAL_MS);
      } catch (err) {
        if (ignore) return;
        // 轮询失败不崩——记错误后退避重试（网络抖动 / 后端临时不可达）。
        // 首次加载失败才显示 error（detail 还是 null 时）；后续失败保留旧 detail。
        const msg = err instanceof Error ? err.message : "trace 查询失败";
        if (detailRef.current === null) {
          setError(msg);
          setLoading(false);
        }
        // 退避后继续轮询（可能是临时网络问题，下个 tick 恢复）。
        timer = setTimeout(poll, ERROR_BACKOFF_MS);
      }
    };

    // 首屏立即拉一次（不等 1s），然后由 poll 自己安排后续。
    setLoading(true);
    setError(null);
    setDetail(null);
    detailRef.current = null;
    poll();

    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [traceId]);

  return { detail, isLive, activeSession, loading, error };
}

// 向后兼容别名：旧调用方 useTraceStream(...) 仍可用，等价于 useTracePolling。
// Phase 4 删除 SSE 时会统一改名。
export const useTraceStream = useTracePolling;
