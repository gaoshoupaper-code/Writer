"use client";

/**
 * 进化运行信息页（/sessions?id=xxx，替换旧 adapt 驾驶舱）。
 *
 * 单进化 Agent 的实时执行视图：
 *   顶：session 元数据 + 状态
 *   步骤时间线：Agent 调用的每个工具步骤（SSE 实时推送）
 *   报告区：最终对比报告（分数 + 改动 + 是否改进）
 *
 * 数据双源：初始 GET /sessions/{id} 拿已落库数据 + SSE 拿实时步骤。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import {
  fetchSessionDetail,
  subscribeEvolveStream,
} from "@/lib/evolve-api";
import type {
  EvolveSession,
  EvolveStreamEvent,
  EvolveReport,
} from "@/lib/evolve-api";

interface StepRecord {
  tool: string;
  status: string;
  detail?: string;
  timestamp: number;
}

export default function EvolveSessionPage() {
  return (
    <Suspense
      fallback={
        <div className="text-dim" style={{ padding: 48 }}>
          加载中…
        </div>
      }
    >
      <EvolveInner />
    </Suspense>
  );
}

function EvolveInner() {
  const searchParams = useSearchParams();
  const sessionId = searchParams.get("id") ?? "";

  const [detail, setDetail] = useState<EvolveSession | null>(null);
  const [status, setStatus] = useState<string>("loading");
  const [steps, setSteps] = useState<StepRecord[]>([]);
  const [logs, setLogs] = useState<string[]>([]);
  const [report, setReport] = useState<EvolveReport | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const cleanRef = useRef<(() => void) | null>(null);

  const loadDetail = useCallback(async () => {
    if (!sessionId) return;
    try {
      const d = await fetchSessionDetail(sessionId);
      setDetail(d);
      setStatus(d.status);
      if (d.report) setReport(d.report);
    } catch {
      setStatus("failed");
    }
  }, [sessionId]);

  useEffect(() => {
    loadDetail();
  }, [loadDetail]);

  // SSE 订阅
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      if (cancelled) return;
      if (status === "done" || status === "failed") return;

      const clean = subscribeEvolveStream(
        sessionId,
        (evt) => handleEvent(evt),
        () => {
          setStreamError("实时连接中断（进程可能重启）");
        },
      );
      cleanRef.current = clean;
    }, 300);

    return () => {
      cancelled = true;
      clearTimeout(timer);
      cleanRef.current?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, status]);

  const handleEvent = (evt: EvolveStreamEvent) => {
    setStreamError(null);
    switch (evt.type) {
      case "start":
        setStatus("running");
        break;
      case "step": {
        const detailParts: string[] = [];
        if ("trace_id" in evt && evt.trace_id) detailParts.push(`trace=${evt.trace_id}`);
        if ("overall" in evt && typeof evt.overall === "number")
          detailParts.push(`score=${evt.overall}`);
        if ("file_count" in evt && typeof evt.file_count === "number")
          detailParts.push(`${evt.file_count} 文件`);
        if ("error" in evt && evt.error) detailParts.push(String(evt.error));
        if ("reason" in evt && evt.reason) detailParts.push(String(evt.reason));
        setSteps((prev) => [
          ...prev,
          {
            tool: evt.tool,
            status: evt.status,
            detail: detailParts.join(" · ") || undefined,
            timestamp: Date.now(),
          },
        ]);
        break;
      }
      case "log":
        setLogs((prev) => [...prev, evt.message]);
        break;
      case "report":
        setReport(evt.report);
        break;
      case "end":
        setStatus(evt.outcome);
        break;
      case "error":
        setStatus("failed");
        setStreamError(evt.reason);
        break;
    }
  };

  if (!sessionId) {
    return (
      <div className="card">
        <div className="empty-state">
          <h3>缺少 session id</h3>
          <p>
            请从{" "}
            <a href="/" className="text-dim" style={{ color: "var(--accent)" }}>
              进化总览
            </a>{" "}
            启动一次进化
          </p>
        </div>
      </div>
    );
  }

  const isRunning = status === "running";
  const baselineScore = report?.baseline_score ?? detail?.baseline_score;
  const candidateScore = report?.candidate_score ?? detail?.candidate_score;
  const improved = report?.improved;

  return (
    <div className="cockpit">
      {/* 顶栏 */}
      <div className="cockpit-topbar">
        <div className="cockpit-title-block">
          <a href="/" className="cockpit-back mono text-mute">
            ← 总览
          </a>
          <h1 className="cockpit-title">
            进化 Session <span className="mono">{sessionId}</span>
          </h1>
          <div className="cockpit-meta mono text-dim">
            case: {detail?.case_id ?? "—"}
          </div>
        </div>
        <div className="cockpit-topright">
          <StatusBadge status={status} />
        </div>
      </div>

      {streamError && (
        <div className="error-box" style={{ marginBottom: 16 }}>
          {streamError}。已落库的数据仍可查看。
        </div>
      )}

      {/* 分数对比卡 */}
      {(baselineScore != null || candidateScore != null) && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="section-head">
            <h2 className="section-title">分数对比</h2>
          </div>
          <div style={{ display: "flex", gap: 24, padding: "8px 0" }}>
            <ScoreBlock label="Baseline" value={baselineScore} />
            <ScoreBlock label="Candidate" value={candidateScore} highlight={improved === true} />
            {baselineScore != null && candidateScore != null && (
              <div style={{ flex: 1, textAlign: "center", alignSelf: "center" }}>
                <div className="text-mute mono" style={{ fontSize: 11 }}>
                  Δ
                </div>
                <div
                  className="mono"
                  style={{
                    fontSize: 20,
                    color:
                      candidateScore > baselineScore
                        ? "var(--completed)"
                        : candidateScore < baselineScore
                          ? "var(--cancelled)"
                          : "var(--text-dim)",
                  }}
                >
                  {(candidateScore - baselineScore >= 0 ? "+" : "") +
                    (candidateScore - baselineScore).toFixed(4)}
                </div>
                <div className="text-mute" style={{ fontSize: 11 }}>
                  {improved === true ? "↑ 改进" : improved === false ? "↓ 未改进" : ""}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 步骤时间线 */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="section-head">
          <h2 className="section-title">执行步骤</h2>
          <span className="text-mute mono" style={{ fontSize: 11 }}>
            {steps.length} 步
          </span>
        </div>
        <div style={{ maxHeight: 400, overflowY: "auto" }}>
          {steps.length === 0 && (
            <div className="empty-state">
              <p className="text-dim" style={{ fontSize: 13 }}>
                {isRunning ? "等待 Agent 启动…" : "无步骤记录"}
              </p>
            </div>
          )}
          {steps.map((step, i) => (
            <StepRow key={i} step={step} index={i} />
          ))}
        </div>
      </div>

      {/* Agent 日志 */}
      {logs.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="section-head">
            <h2 className="section-title">Agent 日志</h2>
          </div>
          <div style={{ maxHeight: 200, overflowY: "auto", padding: "8px 16px" }}>
            {logs.map((log, i) => (
              <div
                key={i}
                className="text-dim mono"
                style={{ fontSize: 12, padding: "2px 0" }}
              >
                {log}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 最终报告 */}
      {report && (
        <div className="card">
          <div className="section-head">
            <h2 className="section-title">进化报告</h2>
            <span
              className="status-badge"
              style={{
                color: report.improved ? "var(--completed)" : "var(--cancelled)",
                background: report.improved
                  ? "rgba(63,185,80,.1)"
                  : "rgba(139,148,158,.1)",
              }}
            >
              {report.improved ? "已改进" : "未改进"}
            </span>
          </div>
          <div className="prose-doc" style={{ padding: "8px 16px" }}>
            <pre
              className="landscape-text"
              style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}
            >
              {report.content}
            </pre>
          </div>
          <div
            style={{
              padding: "12px 16px",
              borderTop: "1px solid var(--border)",
              fontSize: 12,
            }}
            className="text-mute"
          >
            人 review 后，在工作区执行{" "}
            <code className="mono">git commit</code> 采纳改动，或{" "}
            <code className="mono">git reset --hard</code> 丢弃。
          </div>
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; color: string }> = {
    running: { label: "运行中", color: "var(--accent)" },
    done: { label: "完成", color: "var(--completed)" },
    failed: { label: "失败", color: "var(--cancelled)" },
    loading: { label: "加载中", color: "var(--text-dim)" },
  };
  const cfg = map[status] || { label: status, color: "var(--text-dim)" };
  return (
    <span
      className="status-badge mono"
      style={{ color: cfg.color, border: `1px solid ${cfg.color}40` }}
    >
      {cfg.label}
    </span>
  );
}

function ScoreBlock({
  label,
  value,
  highlight,
}: {
  label: string;
  value: number | null | undefined;
  highlight?: boolean;
}) {
  return (
    <div style={{ flex: 1, textAlign: "center" }}>
      <div className="text-mute mono" style={{ fontSize: 11 }}>
        {label}
      </div>
      <div
        className="mono"
        style={{
          fontSize: 24,
          color: highlight ? "var(--completed)" : "var(--text)",
          fontWeight: highlight ? 600 : 400,
        }}
      >
        {value != null ? value.toFixed(4) : "—"}
      </div>
    </div>
  );
}

function StepRow({ step, index }: { step: StepRecord; index: number }) {
  const statusStyle: Record<string, { color: string; icon: string }> = {
    running: { color: "var(--accent)", icon: "▶" },
    done: { color: "var(--completed)", icon: "✓" },
    failed: { color: "var(--cancelled)", icon: "✗" },
    blocked: { color: "var(--warn)", icon: "⚠" },
  };
  const cfg = statusStyle[step.status] || { color: "var(--text-dim)", icon: "·" };
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "8px 16px",
        borderBottom: index > 0 ? "1px solid var(--border)" : "none",
      }}
    >
      <span className="mono" style={{ color: cfg.color, width: 16 }}>
        {cfg.icon}
      </span>
      <span className="mono" style={{ fontSize: 13, minWidth: 120 }}>
        {step.tool}
      </span>
      <span className="text-dim" style={{ fontSize: 12, flex: 1 }}>
        {step.detail || step.status}
      </span>
      <span className="text-mute mono" style={{ fontSize: 10 }}>
        {new Date(step.timestamp).toLocaleTimeString()}
      </span>
    </div>
  );
}
