"use client";

/**
 * 评估详情（/evaluation?id=xxx）。
 *
 * SSE 实时进度（step/log 事件）+ 完成后展示 scores/findings/report_md。
 * running 时自动重载详情（4s 轮询）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { fetchEvalDetail, subscribeEvalStream } from "@/lib/evolve-api";
import type { EvalSession, EvalStreamEvent } from "@/lib/evolve-api";

export function EvaluationDetail({ evalId }: { evalId: string }) {
  const [detail, setDetail] = useState<EvalSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [events, setEvents] = useState<EvalStreamEvent[]>([]);
  const [streamEnded, setStreamEnded] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await fetchEvalDetail(evalId);
      setDetail(d);
      if (d.status !== "running") {
        if (pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      }
    } catch {
      // 忽略
    } finally {
      setLoading(false);
    }
  }, [evalId]);

  useEffect(() => {
    load();
  }, [load]);

  // SSE 订阅
  useEffect(() => {
    setEvents([]);
    setStreamEnded(false);
    const cleanup = subscribeEvalStream(
      evalId,
      (evt) => {
        setEvents((prev) => [...prev, evt]);
        if (evt.type === "end" || evt.type === "error") {
          setStreamEnded(true);
          // 终态后重载详情拿最终 scores/findings
          setTimeout(load, 500);
        }
        if (evt.type === "step" && evt.status === "done") {
          // step 完成也触发详情刷新（write_eval_report 后 status 变 done）
          setTimeout(load, 500);
        }
      },
      () => setStreamEnded(true),
    );
    return cleanup;
  }, [evalId, load]);

  // running 时轮询详情
  useEffect(() => {
    if (detail && detail.status === "running" && !streamEnded) {
      pollRef.current = setInterval(load, 4000);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [detail, streamEnded, load]);

  if (loading) return <div className="text-dim" style={{ padding: 48 }}>加载中…</div>;
  if (!detail) return <div className="text-dim">评估 {evalId} 不存在</div>;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 16, marginBottom: 24 }}>
        <Link href="/evaluation" className="text-dim" style={{ textDecoration: "none" }}>
          ← 返回列表
        </Link>
        <h1 style={{ margin: 0 }}>评估 {evalId}</h1>
        <span className="mono text-dim">trace: {detail.trace_id}</span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
        {/* 左：执行进度（SSE） */}
        <div>
          <h3>执行进度</h3>
          <div className="event-log" style={{ maxHeight: 500, overflowY: "auto" }}>
            {events.length === 0 && !streamEnded && <div className="text-dim">等待事件…</div>}
            {events.map((evt, i) => (
              <EventRow key={i} evt={evt} />
            ))}
            {streamEnded && <div className="text-dim" style={{ marginTop: 8 }}>— 流结束 —</div>}
          </div>
        </div>

        {/* 右：评估结果 */}
        <div>
          <h3>评估结果</h3>
          {detail.status === "running" && <div className="text-dim">评估进行中…</div>}
          {detail.status === "failed" && <div style={{ color: "var(--danger)" }}>评估失败</div>}
          {detail.status === "done" && (
            <div>
              {/* 诊断条目 */}
              {detail.findings && detail.findings.length > 0 && (
                <div style={{ marginBottom: 16 }}>
                  <div className="text-dim" style={{ fontSize: 13, marginBottom: 8 }}>
                    诊断条目（{detail.findings.length} 条）
                  </div>
                  {detail.findings.map((f, i) => (
                    <div key={f.id ?? i} className="finding-card" style={{ marginBottom: 8, padding: "8px 12px", borderLeft: `3px solid ${sevColor(f.severity)}` }}>
                      <div style={{ display: "flex", gap: 8, fontSize: 12 }}>
                        {f.id && <span className="mono text-dim" style={{ fontSize: 11 }}>{f.id}</span>}
                        <span style={{ color: sevColor(f.severity), fontWeight: 600 }}>{f.severity.toUpperCase()}</span>
                        <span className="text-dim">{f.dimension}</span>
                        <span className="text-dim">· {f.evidence_type}</span>
                      </div>
                      <div style={{ marginTop: 4 }}>{f.finding}</div>
                      {f.evidence && <div className="text-dim" style={{ fontSize: 12, marginTop: 2 }}>证据：{f.evidence}</div>}
                    </div>
                  ))}
                </div>
              )}

              {/* 报告全文 */}
              {detail.report_md && (
                <details>
                  <summary className="text-dim" style={{ cursor: "pointer", fontSize: 13 }}>报告全文</summary>
                  <pre className="report-md" style={{ whiteSpace: "pre-wrap", fontSize: 12, maxHeight: 400, overflowY: "auto" }}>
                    {detail.report_md}
                  </pre>
                </details>
              )}

              {/* 进化入口 */}
              <div style={{ marginTop: 16, padding: 12, background: "var(--bg-elev)", borderRadius: 6 }}>
                <div className="text-dim" style={{ fontSize: 13, marginBottom: 8 }}>
                  评估完成。可基于此评估报告启动进化（产新 Agent 版本）。
                </div>
                <Link href={`/evolve?trace=${encodeURIComponent(detail.trace_id)}`} className="btn-primary" style={{ display: "inline-block", textDecoration: "none" }}>
                  去进化这条 trace →
                </Link>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function EventRow({ evt }: { evt: EvalStreamEvent }) {
  if (evt.type === "step") {
    return (
      <div className="event-row">
        <span className="event-status" style={{ color: stepColor(evt.status) }}>●</span>
        <span className="mono" style={{ fontSize: 12 }}>{evt.tool}</span>
        <span className="text-dim" style={{ fontSize: 11 }}>{evt.status}</span>
      </div>
    );
  }
  if (evt.type === "log") {
    return (
      <div className="event-row" style={{ color: "var(--text-dim)" }}>
        <span>💬</span>
        <span style={{ fontSize: 12 }}>{evt.message}</span>
      </div>
    );
  }
  if (evt.type === "heartbeat") return null;
  if (evt.type === "start") return <div className="event-row text-dim">— 开始 —</div>;
  if (evt.type === "end") return <div className="event-row" style={{ color: "var(--accent)" }}>— 完成 —</div>;
  if (evt.type === "error") return <div className="event-row" style={{ color: "var(--danger)" }}>✗ {evt.reason}</div>;
  return null;
}

function stepColor(status: string): string {
  if (status === "done") return "var(--accent)";
  if (status === "failed") return "var(--danger)";
  if (status === "running") return "var(--warn)";
  return "var(--text-dim)";
}

function sevColor(sev: string): string {
  if (sev === "high") return "var(--danger)";
  if (sev === "medium") return "var(--warn)";
  return "var(--text-dim)";
}
