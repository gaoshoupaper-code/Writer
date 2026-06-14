"use client";

import { useEffect, useMemo, useState } from "react";
import { ReactFlow, Background, Controls, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { fetchWorkspaceStorylineGraph } from "../../lib/api";
import type { WorkspaceStorylineGraphContent } from "../../lib/types";

// 布局参数：纵轴每个时间槽一行，泳道每列固定宽
const COLUMN_WIDTH = 230;
const ROW_HEIGHT = 72;

const LANE_COLORS: { keyword: string; bg: string; label: string }[] = [
  { keyword: "主线", bg: "#4a90d9", label: "主线" },
  { keyword: "暗线", bg: "#9b9b9b", label: "暗线" },
  { keyword: "支线", bg: "#7ac17a", label: "支线" },
  { keyword: "角色", bg: "#d98a4a", label: "角色线" },
];

function laneColor(type: string): string {
  return (LANE_COLORS.find((c) => type.includes(c.keyword)) ?? { bg: "#cccccc" }).bg;
}

type StorylinePanelProps = { workspaceId: string };

/** 故事线时间轴：纵轴=t_map 时间序（统一对齐），主线居中贯穿，支线左右并行，交汇红框。 */
export function StorylinePanel({ workspaceId }: StorylinePanelProps) {
  const [data, setData] = useState<WorkspaceStorylineGraphContent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchWorkspaceStorylineGraph(workspaceId)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  const { nodes, edges } = useMemo(() => {
    if (!data || data.storylines.length === 0) return { nodes: [] as Node[], edges: [] as Edge[] };

    // 主属泳道：每个事件首次被某条线 key_events 引用的那条线
    const primaryLane: Record<string, string> = {};
    for (const sl of data.storylines) {
      for (const eid of sl.key_events) {
        if (!(eid in primaryLane)) primaryLane[eid] = sl.id;
      }
    }

    // 泳道 x：主线居中（x=0），其余左右交替并行
    const mainIdx = Math.max(0, data.storylines.findIndex((s) => s.type.includes("主线")));
    const laneX: Record<string, number> = {};
    data.storylines.forEach((sl, idx) => {
      if (idx === mainIdx) laneX[sl.id] = 0;
    });
    data.storylines
      .filter((_, i) => i !== mainIdx)
      .forEach((sl, i) => {
        const sign = i % 2 === 0 ? 1 : -1;
        laneX[sl.id] = sign * (Math.floor(i / 2) + 1) * COLUMN_WIDTH;
      });

    const laneTypeOf = (lid: string) => data.storylines.find((s) => s.id === lid)?.type ?? "";

    // 节点：y = t_map * 行高（统一纵轴 → 时间对齐）
    const nodes: Node[] = Object.values(data.events).map((ev) => {
      const lid = primaryLane[ev.id] ?? data.storylines[0].id;
      const t = data.t_map[ev.id] ?? 0;
      const isCross = ev.storylines.length >= 2;
      return {
        id: ev.id,
        position: { x: laneX[lid] ?? 0, y: t * ROW_HEIGHT },
        data: { label: `T${String(t).padStart(2, "0")} · ${ev.id} · ${ev.name || "未命名"} · ${ev.type || "—"}` },
        style: {
          background: laneColor(laneTypeOf(lid)),
          color: "#fff",
          fontSize: 12,
          width: 200,
          ...(isCross ? { borderColor: "#e8470b", borderWidth: 3 } : {}),
        },
      };
    });

    // 边：每条线按 key_events 顺序串联（同泳道纵向）
    const edges: Edge[] = [];
    for (const sl of data.storylines) {
      for (let i = 0; i < sl.key_events.length - 1; i++) {
        const src = sl.key_events[i];
        const tgt = sl.key_events[i + 1];
        if (!data.events[src] || !data.events[tgt]) continue;
        edges.push({
          id: `${sl.id}-${src}-${tgt}`,
          source: src,
          target: tgt,
          type: "smoothstep",
          style: { stroke: "#999", strokeWidth: 1.5 },
        });
      }
    }
    return { nodes, edges };
  }, [data]);

  return (
    <section className="panel-surface content-panel" aria-label="故事线">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">Storyline</span>
          <h2>故事线时间轴</h2>
        </div>
        {loading ? <span className="outline-state">加载中</span> : null}
      </div>

      <div className="content-panel-body">
        {error ? (
          <p className="status-copy">加载失败：{error}</p>
        ) : loading ? (
          <p className="status-copy">加载故事线图…</p>
        ) : !data || data.storylines.length === 0 ? (
          <p className="status-copy">
            暂无故事线数据。运行故事构建（storybuilding）生成故事线后，此处将展示竖向时间轴：纵轴 = 时间（T01→T##），主线居中贯穿，支线/角色线/暗线左右并行，交汇事件红框高亮。
          </p>
        ) : (
          <>
            {data.stale ? <p className="status-copy">提示：故事线图刚按需重新生成。</p> : null}
            <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center", marginBottom: 8, fontSize: 13 }}>
              {LANE_COLORS.map((c) => (
                <span key={c.keyword} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                  <span style={{ display: "inline-block", width: 14, height: 14, background: c.bg, borderRadius: 3 }} />
                  {c.label}
                </span>
              ))}
              <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                <span style={{ display: "inline-block", width: 14, height: 14, background: "#fff3e6", border: "3px solid #e8470b", borderRadius: 3, boxSizing: "border-box" }} />
                交汇事件
              </span>
              <span style={{ color: "#888" }}>纵轴 T## = 故事内时间先后 · 滚轮缩放 · 拖拽平移</span>
            </div>
            <div className="storyline-flow" style={{ height: "70vh", minHeight: 480 }}>
              <ReactFlow
                nodes={nodes}
                edges={edges}
                fitView
                minZoom={0.05}
                maxZoom={2}
                nodesDraggable={false}
                nodesConnectable={false}
              >
                <Background />
                <Controls />
              </ReactFlow>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
