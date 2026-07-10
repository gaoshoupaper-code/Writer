// useTraceStream —— trace 详情实时生命周期 hook（Phase 2 T6 重构）。
//
// 数据流（后端投影 + SSE node patch）：
//   1. getTraceDetail 拿 evolution.db 投影结果（run + nodes + todos，轻量）
//   2. 探活：trace 是否在活跃集合（getActiveRuns 命中）
//   3. 活跃 → 开 evoTraceStream，按 source 分流：
//      - evolution 源：收 NodePatch → applyNodePatch（append/update，O(A+U)）
//      - executor 源：收全量 snapshot 强制对齐（终态 T9）
//   4. 非活跃 → 纯展示 db 数据，不开 SSE
//   5. SSE event:end（终态）或 event:error → 关闭流，保留已投影数据
//
// 变更：前端不再投影（删掉 appendLiveTraceEvent / projectTraceDetail）。
// 投影完全由后端负责，前端只收 patch 做轻量数组操作，消除 O(N²)。

import { useCallback, useEffect, useRef, useState } from "react";
import { getTraceDetail, getActiveRuns } from "@/lib/api";
import { evoTraceStream } from "@/lib/stream";
import { applyNodePatch, applyNodeSnapshot } from "@/lib/trace";
import type { TraceDetailLite, NodePatch, TraceNode, TraceRunSummary } from "@/lib/types";

type TraceStreamState = {
  detail: TraceDetailLite | null;
  isLive: boolean;
  loading: boolean;
  error: string | null;
};

export function useTraceStream(traceId: string | null): TraceStreamState {
  const [detail, setDetail] = useState<TraceDetailLite | null>(null);
  const [isLive, setIsLive] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 持有当前 detail 的可变引用，供 SSE 回调增量更新（避免闭包旧值）
  const detailRef = useRef<TraceDetailLite | null>(null);

  const updateDetail = useCallback(
    (updater: (prev: TraceDetailLite | null) => TraceDetailLite | null) => {
      setDetail((prev) => {
        const next = updater(prev);
        detailRef.current = next;
        return next;
      });
    },
    [],
  );

  useEffect(() => {
    if (!traceId) {
      setDetail(null);
      setLoading(false);
      return;
    }

    let ignore = false;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setDetail(null);
    detailRef.current = null;

    (async () => {
      // ── 1. db 打底 ──
      try {
        const base = await getTraceDetail(traceId);
        if (ignore) return;
        updateDetail(() => base);
      } catch (err) {
        // db 无此 trace（活跃但未摄入的空窗）→ 不致命，继续探活
        if (!ignore) {
          setError(err instanceof Error ? err.message : "trace 未找到");
        }
      }

      // ── 2. 探活 ──
      let active = false;
      try {
        const runs = await getActiveRuns();
        active = runs.some((r) => r.trace_id === traceId);
      } catch {
        active = false;
      }
      if (ignore) return;
      setIsLive(active);

      if (!active) {
        setLoading(false);
        return;
      }

      // ── 3. 活跃 → 开 SSE 收增量 ──
      setLoading(false);

      try {
        const gen = evoTraceStream(`/api/traces/${traceId}/stream`, { method: "GET" });
        for await (const frame of gen) {
          if (cancelled || ignore) break;

          // Phase 2 T5：data 帧按 source 分流
          if (frame.event === "data") {
            const envelope = frame.data as {
              _type?: string;
              source?: string;
              data?: unknown;
            };

            // 新信封格式（带 _type/source）
            if (envelope && envelope._type) {
              if (envelope._type === "data") {
                if (envelope.source === "evolution") {
                  // evolution 源：node patch
                  const patch = envelope.data as NodePatch;
                  updateDetail((prev) => applyNodePatch(prev, patch));
                }
                // executor 源：旧逻辑不处理（executor trace 走全量展示）
              } else if (envelope._type === "snapshot") {
                if (envelope.source === "evolution") {
                  // evolution 终态 snapshot：全量替换 nodes
                  const nodes = envelope.data as TraceNode[];
                  updateDetail((prev) => applyNodeSnapshot(prev, nodes));
                } else {
                  // executor snapshot：run 状态校准
                  const snap = envelope.data as { status?: string };
                  updateDetail((prev) => {
                    if (!prev) return prev;
                    return {
                      ...prev,
                      run: {
                        ...prev.run,
                        status: (snap.status as TraceRunSummary["status"]) ?? prev.run.status,
                      },
                    };
                  });
                }
              }
              continue;
            }

            // 兜底：旧格式帧（无 _type，直接是事件或 patch）
            // 后端升级前的过渡兼容，后续可删除
            continue;
          }

          if (frame.event === "snapshot") {
            // 旧格式 snapshot（带 event: snapshot 行）—— run 状态校准
            const snap = frame.data as { status?: string };
            updateDetail((prev) => {
              if (!prev) return prev;
              return {
                ...prev,
                run: {
                  ...prev.run,
                  status: (snap.status as TraceRunSummary["status"]) ?? prev.run.status,
                },
              };
            });
            continue;
          }

          if (frame.event === "end") {
            setIsLive(false);
            return;
          }

          if (frame.event === "error") {
            setIsLive(false);
            return;
          }
        }
      } catch {
        if (!ignore) setIsLive(false);
      }
    })();

    return () => {
      ignore = true;
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [traceId]);

  return { detail, isLive, loading, error };
}
