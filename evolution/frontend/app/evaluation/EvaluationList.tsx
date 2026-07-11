"use client";

/**
 * 评估记录列表（/evaluation 无 ?id= 时）。
 *
 * 启动评估入口（选 trace → POST /eval-agent/start → 跳详情）+ 评估记录列表。
 * running 行定时轮询刷新（4s）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { fetchEvalSessions, startEval } from "@/lib/evolve-api";
import type { EvalSession } from "@/lib/evolve-api";
import { fetchTraces } from "@/lib/monitor-api";
import type { TraceListItem } from "@/lib/types";

export function EvaluationList() {
  const [evals, setEvals] = useState<EvalSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showStart, setShowStart] = useState(false);
  const [starting, setStarting] = useState(false);

  // 启动对话框：trace 选择
  const [traces, setTraces] = useState<TraceListItem[]>([]);
  const [traceLoading, setTraceLoading] = useState(false);
  const [selectedTraceId, setSelectedTraceId] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchEvalSessions();
      setEvals(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 有进行中评估时轮询
  const hasRunning = evals.some((e) => e.status === "running");
  useEffect(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (hasRunning) {
      pollRef.current = setInterval(load, 4000);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [hasRunning, load]);

  const loadTraces = useCallback(async () => {
    setTraceLoading(true);
    try {
      const data = await fetchTraces({ limit: 100 });
      // 排除进化端自观测 trace——评估 Agent 只评估创作 Agent 的 trace，
      // 不能评估自己（evolution_eval）或进化 Agent（evolution_evolve）的录像。
      const filtered = data.filter(
        (t) => t.run_purpose !== "evolution_eval" && t.run_purpose !== "evolution_evolve",
      );
      setTraces(filtered);
      if (filtered.length > 0 && !selectedTraceId) {
        setSelectedTraceId(filtered[0].trace_id);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载 trace 失败");
    } finally {
      setTraceLoading(false);
    }
  }, [selectedTraceId]);

  const handleStart = async () => {
    if (!selectedTraceId) return;
    setStarting(true);
    setError(null);
    try {
      const resp = await startEval(selectedTraceId);
      window.location.href = `/evaluation?id=${encodeURIComponent(resp.eval_id)}`;
    } catch (e) {
      setError(e instanceof Error ? e.message : "启动评估失败");
    } finally {
      setStarting(false);
    }
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 16, marginBottom: 24 }}>
        <h1 style={{ margin: 0 }}>评估</h1>
        <span className="text-dim">独立评估 Agent · 对一条 trace 做诊断（评分+问题+证据）</span>
        <div style={{ flex: 1 }} />
        <button
          className="btn-primary"
          onClick={() => {
            setShowStart(true);
            loadTraces();
          }}
        >
          启动评估
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {showStart && (
        <div className="modal-overlay" onClick={() => !starting && setShowStart(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 640 }}>
            <h3 style={{ marginTop: 0 }}>启动评估</h3>
            <p className="text-dim" style={{ fontSize: 13 }}>
              选择一条 trace，评估 Agent 将诊断其 Agent 运行流程 + 内容质量。
            </p>
            <label className="field-label">选择 trace</label>
            {traceLoading ? (
              <div className="text-dim">加载 trace 列表…</div>
            ) : (
              <select
                className="field-select"
                value={selectedTraceId}
                onChange={(e) => setSelectedTraceId(e.target.value)}
                size={8}
                style={{ width: "100%" }}
              >
                {traces.map((t) => (
                  <option key={t.trace_id} value={t.trace_id}>
                    {t.trace_id} · {t.status} · {t.session_name || "(无名)"}
                  </option>
                ))}
              </select>
            )}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
              <button className="btn-ghost" onClick={() => setShowStart(false)} disabled={starting}>
                取消
              </button>
              <button className="btn-primary" onClick={handleStart} disabled={starting || !selectedTraceId}>
                {starting ? "启动中…" : "开始评估"}
              </button>
            </div>
          </div>
        </div>
      )}

      {loading ? (
        <div className="text-dim">加载中…</div>
      ) : evals.length === 0 ? (
        <div className="empty-state">
          <div className="text-dim">还没有评估记录</div>
          <div className="text-dim" style={{ fontSize: 13, marginTop: 8 }}>
            先去「手动测试」跑一条 trace，再回来评估
          </div>
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>eval_id</th>
              <th>trace</th>
              <th>版本</th>
              <th>状态</th>
              <th>诊断数</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {evals.map((e) => (
              <tr
                key={e.eval_id}
                onClick={() => (window.location.href = `/evaluation?id=${encodeURIComponent(e.eval_id)}`)}
                style={{ cursor: "pointer" }}
              >
                <td className="mono">{e.eval_id}</td>
                <td className="mono">{e.trace_id.slice(0, 16)}</td>
                <td>
                  {e.agent_version_type === "snapshot"
                    ? `v${e.agent_version_id}`
                    : e.agent_version_type || "-"}
                </td>
                <td>
                  <StatusBadge status={e.status} />
                </td>
                <td>{e.findings ? e.findings.length : "-"}</td>
                <td className="text-dim">{e.created_at.slice(0, 19).replace("T", " ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    running: "var(--warn)",
    done: "var(--accent)",
    failed: "var(--danger)",
  };
  const labels: Record<string, string> = {
    running: "进行中",
    done: "完成",
    failed: "失败",
  };
  const color = colors[status] || "var(--text-dim)";
  return (
    <span style={{ color }}>
      <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: color, marginRight: 6, verticalAlign: "middle" }} />
      {labels[status] || status}
    </span>
  );
}
