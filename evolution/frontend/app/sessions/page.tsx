"use client";

/**
 * 进化运行信息页（/sessions?id=xxx）—— 功能③详情（三功能解耦）。
 *
 * 精简后（方案→执行两阶段，不自证比分）：
 *   顶：session 元数据 + 状态（4 态：running/pending_review/published/discarded）
 *   步骤时间线：Agent 调用的每个工具步骤（SSE 实时推送）
 *   待审操作区：pending_review 时显示发版/丢弃按钮
 *
 * 数据双源：初始 GET /sessions/{id} + SSE 实时步骤。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import {
  fetchSessionDetail,
  subscribeEvolveStream,
  publishSession,
  discardSession,
} from "@/lib/evolve-api";
import type { EvolveSession, EvolveStreamEvent } from "@/lib/evolve-api";

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
  const [streamError, setStreamError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState<"publish" | "discard" | null>(null);
  const cleanRef = useRef<(() => void) | null>(null);

  const loadDetail = useCallback(async () => {
    if (!sessionId) return;
    try {
      const d = await fetchSessionDetail(sessionId);
      setDetail(d);
      setStatus(d.status);
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
      if (status === "pending_review" || status === "published" || status === "discarded" || status === "failed") return;

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
        if ("error" in evt && evt.error) detailParts.push(String(evt.error));
        if ("path" in evt && evt.path) detailParts.push(String(evt.path));
        if ("changes" in evt && typeof evt.changes === "number")
          detailParts.push(`${evt.changes} 改动`);
        if ("findings" in evt && typeof evt.findings === "number")
          detailParts.push(`${evt.findings} 诊断`);
        setSteps((prev) => [
          ...prev,
          {
            tool: evt.tool,
            status: evt.status,
            detail: detailParts.join(" · ") || undefined,
            timestamp: Date.now(),
          },
        ]);
        // write_change_log/write_design_doc 完成后刷新详情
        if (evt.status === "done") {
          setTimeout(loadDetail, 500);
        }
        break;
      }
      case "log":
        setLogs((prev) => [...prev, evt.message]);
        break;
      case "end":
        setTimeout(loadDetail, 500);
        break;
      case "error":
        setStatus("failed");
        setStreamError(evt.reason);
        break;
    }
  };

  const handlePublish = async () => {
    if (!sessionId) return;
    setActing("publish");
    setActionError(null);
    try {
      await publishSession(sessionId);
      await loadDetail();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "发版失败");
    } finally {
      setActing(null);
    }
  };

  const handleDiscard = async () => {
    if (!sessionId) return;
    if (!confirm("确认丢弃？working 区将回退到上一 production 版本。")) return;
    setActing("discard");
    setActionError(null);
    try {
      await discardSession(sessionId);
      await loadDetail();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "丢弃失败");
    } finally {
      setActing(null);
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
  const isPendingReview = status === "pending_review";

  return (
    <div className="cockpit">
      {/* 顶栏 */}
      <div className="cockpit-topbar">
        <div className="cockpit-title-block">
          <a href="/evolve" className="cockpit-back mono text-mute">
            ← 进化
          </a>
          <h1 className="cockpit-title">
            进化 Session <span className="mono">{sessionId}</span>
          </h1>
          <div className="cockpit-meta mono text-dim">
            trace: {detail?.baseline_trace || detail?.trace_id || "—"}
            {detail?.eval_ref && ` · 评估: ${detail.eval_ref}`}
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

      {actionError && (
        <div className="error-box" style={{ marginBottom: 16 }}>
          {actionError}
        </div>
      )}

      {/* 待审操作区：pending_review 时显示发版/丢弃按钮 */}
      {isPendingReview && (
        <div className="card" style={{ marginBottom: 16, borderLeft: "3px solid var(--accent)" }}>
          <div style={{ padding: "12px 16px" }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>改动已落地，等待 review</div>
            <div className="text-dim" style={{ fontSize: 13, marginBottom: 12 }}>
              进化 Agent 已产出代码改动（方案→执行两阶段）。
              <strong>发版</strong>会固化为新 Agent 版本（git commit + 快照），
              <strong>丢弃</strong>会回退 working 区到上一版本。
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button className="btn-primary" onClick={handlePublish} disabled={acting !== null}>
                {acting === "publish" ? "发版中…" : "✓ 发版"}
              </button>
              <button className="btn-ghost" onClick={handleDiscard} disabled={acting !== null}>
                {acting === "discard" ? "丢弃中…" : "✗ 丢弃"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 已发版提示 */}
      {status === "published" && (
        <div className="card" style={{ marginBottom: 16, borderLeft: "3px solid var(--completed)" }}>
          <div style={{ padding: "12px 16px", color: "var(--completed)" }}>
            ✓ 已发版为新 Agent 版本。可去「版本谱系」查看，或去「手动测试」验证新版本。
          </div>
        </div>
      )}
      {status === "discarded" && (
        <div className="card" style={{ marginBottom: 16, borderLeft: "3px solid var(--text-dim)" }}>
          <div style={{ padding: "12px 16px" }} className="text-dim">
            已丢弃，working 区已回退到上一版本。
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
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; color: string }> = {
    running: { label: "执行中", color: "var(--accent)" },
    pending_review: { label: "待审", color: "var(--warn)" },
    published: { label: "已发版", color: "var(--completed)" },
    discarded: { label: "已丢弃", color: "var(--text-dim)" },
    failed: { label: "失败", color: "var(--cancelled)" },
    done: { label: "完成", color: "var(--completed)" },
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
