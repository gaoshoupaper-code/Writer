import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { getTraceDetail, type TraceDetail } from "@/lib/api";
import { evoSseStream } from "@/lib/stream";

/**
 * Trace 详情页（从 monitor 下钻进入）。
 *
 * 展示一次执行的完整信息：
 * - run 概要（状态/耗时/token）
 * - nodes 树（agent/llm/tool 调用层级）
 * - 实时流（若 trace 仍在运行）
 */
export default function TraceDetailPage() {
  const { traceId } = useParams<{ traceId: string }>();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [liveEvents, setLiveEvents] = useState<any[]>([]);
  const [streaming, setStreaming] = useState(false);

  useEffect(() => {
    if (!traceId) return;
    setLoading(true);
    getTraceDetail(traceId)
      .then((d) => {
        setDetail(d);
        // 若 trace 仍在运行，订阅 SSE
        if (d.run.status === "running" || d.run.status === "awaiting_input") {
          subscribeLive(traceId);
        }
      })
      .catch((err) => toast.error(err instanceof Error ? err.message : "读取 trace 失败"))
      .finally(() => setLoading(false));
  }, [traceId]);

  async function subscribeLive(tid: string) {
    setStreaming(true);
    try {
      const gen = evoSseStream(`/api/traces/${tid}/stream`, { method: "GET" });
      for await (const frame of gen) {
        // trace SSE 有 event 名（snapshot/end/error）+ data 帧
        if (frame.type === "snapshot" || frame.status) continue;
        if (frame.type === "end") {
          setStreaming(false);
          // 刷新详情
          getTraceDetail(tid).then(setDetail).catch(() => {});
          break;
        }
        if (frame.type === "error") {
          setStreaming(false);
          break;
        }
        if (frame.sequence != null) {
          setLiveEvents((prev) => [...prev, frame]);
        }
      }
    } catch {
      setStreaming(false);
    }
  }

  if (loading) return <div className="page-loading">加载 trace…</div>;
  if (!detail) return <div className="monitor-empty">trace 不存在</div>;

  const { run, nodes } = detail;
  const allEvents = [...(detail.events || []), ...liveEvents];

  return (
    <div className="trace-page">
      <header className="page-header">
        <div className="trace-header">
          <button className="back-button" onClick={() => navigate("/")}>← 返回</button>
          <h1>Trace {traceId?.slice(0, 12)}…</h1>
          {streaming && <span className="streaming-badge"><span className="pulse" /> 实时</span>}
        </div>
      </header>

      {/* run 概要 */}
      <section className="trace-summary">
        <div className="summary-item">
          <label>状态</label>
          <span className={`session-status ${run.status}`}>{run.status}</span>
        </div>
        <div className="summary-item">
          <label>耗时</label>
          <span>{run.duration_ms != null ? `${(run.duration_ms / 1000).toFixed(1)}s` : "—"}</span>
        </div>
        <div className="summary-item">
          <label>事件数</label>
          <span>{run.event_count}</span>
        </div>
        <div className="summary-item">
          <label>开始</label>
          <span>{run.started_at?.replace("T", " ").slice(0, 19) || "—"}</span>
        </div>
        {run.error && (
          <div className="summary-item error">
            <label>错误</label>
            <span>{run.error}</span>
          </div>
        )}
      </section>

      {/* nodes 树 */}
      {nodes && nodes.length > 0 && (
        <section className="trace-section">
          <h3>调用树（{nodes.length} 节点）</h3>
          <div className="node-tree">
            {nodes
              .filter((n) => n.depth === 0 || n.depth === 1)
              .map((node) => (
                <div key={node.node_id} className={`node-item depth-${node.depth} kind-${node.kind}`}>
                  <span className="node-kind">{nodeKindIcon(node.kind)}</span>
                  <span className="node-label">{node.label || node.agent_name || node.tool_name || node.kind}</span>
                  {node.duration_ms != null && <span className="node-dur">{(node.duration_ms / 1000).toFixed(1)}s</span>}
                  {node.status === "failed" && <span className="node-fail">✗</span>}
                  {node.model_name && <span className="node-model">{node.model_name}</span>}
                </div>
              ))}
          </div>
          {nodes.length > 20 && (
            <div className="tree-hint">仅展示顶层节点（共 {nodes.length} 个）</div>
          )}
        </section>
      )}

      {/* 事件流 */}
      {allEvents.length > 0 && (
        <section className="trace-section">
          <h3>事件流（{allEvents.length}）</h3>
          <div className="event-stream">
            {allEvents.slice(-100).map((ev: any, i: number) => (
              <div key={i} className={`event-line event-${ev.type}`}>
                <span className="event-seq">#{ev.sequence}</span>
                <span className="event-type">{ev.type}</span>
                {ev.agent_name && <span className="event-agent">{ev.agent_name}</span>}
                {ev.tool_name && <span className="event-tool">{ev.tool_name}</span>}
                {ev.error && <span className="event-error">{ev.error}</span>}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function nodeKindIcon(kind: string): string {
  const map: Record<string, string> = {
    run: "▶", agent: "🤖", llm: "💭", tool: "🔧", skill: "⚡", todo: "📋", error: "❌",
  };
  return map[kind] ?? "•";
}
