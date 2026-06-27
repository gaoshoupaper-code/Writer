// useTraceStream —— trace 详情实时生命周期 hook（D5/D10）
//
// 数据流（D5：evolution.db 打底 + 活的叠 SSE 增量）：
//   1. fetchTraceDetail 拿 evolution.db 全量投影（run + nodes + context + todos + events）
//   2. 探活：trace 是否在活跃集合（fetchActiveRuns 命中）
//   3. 活跃 → 开 EventSource SSE（D9），onmessage 收事件调 appendLiveTraceEvent 投影叠加
//   4. 非活跃 → 纯展示 db 数据，不开 SSE
//   5. SSE event:end（终态）或 event:error → 关闭流，保留已投影数据
//
// 返回 { detail, isLive, loading, error }。
// traceId 变化时重新初始化；组件卸载时关 EventSource。

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchTraceDetail, isTraceActive, traceStreamUrl } from "@/lib/monitor-api";
import { appendLiveTraceEvent } from "@/lib/trace";
import type { TraceDetail, TraceRunSummary } from "@/lib/types";

type TraceStreamState = {
  detail: TraceDetail | null;
  isLive: boolean;
  loading: boolean;
  error: string | null;
};

export function useTraceStream(traceId: string | null): TraceStreamState {
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [isLive, setIsLive] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 持有当前 detail 的可变引用，供 SSE 回调增量更新（避免闭包旧值）
  const detailRef = useRef<TraceDetail | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);

  const updateDetail = useCallback((updater: (prev: TraceDetail | null) => TraceDetail | null) => {
    setDetail((prev) => {
      const next = updater(prev);
      detailRef.current = next;
      return next;
    });
  }, []);

  useEffect(() => {
    if (!traceId) {
      setDetail(null);
      setLoading(false);
      return;
    }

    let ignore = false;
    setLoading(true);
    setError(null);
    setDetail(null);
    detailRef.current = null;

    (async () => {
      // ── 1. db 打底 ──
      let base: TraceDetail | null = null;
      try {
        base = await fetchTraceDetail(traceId);
        if (ignore) return;
        updateDetail(() => base);
      } catch (err) {
        // db 无此 trace（活跃但未摄入的空窗）→ 不致命，继续探活
        if (!ignore) {
          setError(err instanceof Error ? err.message : "trace 未找到");
        }
      }

      // ── 2. 探活 ──
      const active = await isTraceActive(traceId);
      if (ignore) return;
      setIsLive(active);

      if (!active) {
        // 非活跃：纯 db 展示，不开 SSE
        setLoading(false);
        return;
      }

      // ── 3. 活跃 → 开 SSE 叠增量 ──
      const es = new EventSource(traceStreamUrl(traceId));
      eventSourceRef.current = es;
      setLoading(false);

      es.onmessage = (ev) => {
        // D9：逐事件推 executor 原样 TraceLogEvent
        try {
          const event = JSON.parse(ev.data);
          updateDetail((prev) => {
            // 首条事件但 db 空窗（base=null）→ 需构造 fallback run
            if (!prev && base) return appendLiveTraceEvent(base, event, base.run);
            if (!prev) {
              // db 空窗：用事件自身信息构造最小 fallback run
              const fallback: TraceRunSummary = {
                trace_id: event.trace_id || traceId,
                workspace_id: "",
                thread_id: "",
                session_name: "",
                workspace_path: "",
                endpoint: "",
                status: "running",
                started_at: event.timestamp || "",
                event_count: 0,
                path: "",
              };
              return appendLiveTraceEvent(null, event, fallback);
            }
            return appendLiveTraceEvent(prev, event, prev.run);
          });
        } catch {
          // JSON 解析失败忽略（保实时流不被脏数据打断）
        }
      };

      es.addEventListener("snapshot", (ev) => {
        // D9 snapshot：run summary（status/event_count），用于校准
        try {
          const snap = JSON.parse((ev as MessageEvent).data);
          updateDetail((prev) => {
            if (!prev) return prev;
            return {
              ...prev,
              run: { ...prev.run, status: snap.status ?? prev.run.status },
            };
          });
        } catch {
          /* ignore */
        }
      });

      es.addEventListener("end", () => {
        // 终态：trace 完成/失败/取消，关闭流
        setIsLive(false);
        es.close();
      });

      es.addEventListener("error", () => {
        // executor 不可用（404 等）→ 降级纯 db 展示
        setIsLive(false);
        es.close();
      });
    })();

    return () => {
      ignore = true;
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [traceId]);

  return { detail, isLive, loading, error };
}
