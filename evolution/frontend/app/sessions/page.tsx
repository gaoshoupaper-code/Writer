"use client";

/**
 * 进化驾驶舱（/sessions?id=xxx，核心页，需求 §4.2）。
 *
 * 用 query 参数而非动态路由——适配 output:export 静态导出（同项目 traces 模式）。
 *
 * 三栏布局：
 *   顶：session 元数据 + 状态 + 软停按钮（D12）
 *   pipeline 卡：9 节点流水线（SSE 实时推进，D4）
 *   中：当前节点产出（landscape/edits/reward 对比/critic 评语）
 *   底：历轮结果（已落库的轮）
 *
 * 数据双源：初始 GET /sessions/{id} 拿已落库轮 + SSE 拿实时节点产出。
 * 断连策略（D10）：SSE 断了标记"异常终止"，不重连，展示已落库数据。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import {
  fetchSessionDetail,
  stopSession,
  subscribeAdaptStream,
} from "@/lib/adapt-api";
import { AdaptPipeline } from "@/components/adapt/AdaptPipeline";
import { NodeOutputView } from "@/components/adapt/NodeOutputView";
import { SessionStatusBadge } from "@/components/adapt/SessionStatusBadge";
import type {
  AdaptNodeName,
  AdaptRound,
  AdaptSessionDetail,
  AdaptStreamEvent,
  NodeOutputPayload,
  SessionStatus,
} from "@/lib/adapt-types";

// 节点产出缓冲：每节点最近一次产出
type OutputMap = Partial<
  Record<AdaptNodeName, { payload: NodeOutputPayload; round: number }>
>;

export default function CockpitPage() {
  return (
    <Suspense fallback={<div className="text-dim" style={{ padding: 48 }}>加载中…</div>}>
      <CockpitInner />
    </Suspense>
  );
}

function CockpitInner() {
  const searchParams = useSearchParams();
  const sessionId = searchParams.get("id") ?? "";

  const [detail, setDetail] = useState<AdaptSessionDetail | null>(null);
  const [status, setStatus] = useState<SessionStatus | "loading">("loading");
  const [currentNode, setCurrentNode] = useState<AdaptNodeName | null>(null);
  const [outputs, setOutputs] = useState<OutputMap>({});
  const [completed, setCompleted] = useState<Set<AdaptNodeName>>(new Set());
  const [round, setRound] = useState(0);
  const [revisionActive, setRevisionActive] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [stopBusy, setStopBusy] = useState(false);
  const cleanRef = useRef<(() => void) | null>(null);

  // 1. 初始加载已落库详情
  const loadDetail = useCallback(async () => {
    if (!sessionId) return;
    try {
      const d = await fetchSessionDetail(sessionId);
      setDetail(d);
      setStatus(d.status);
    } catch {
      setStatus("error");
    }
  }, [sessionId]);

  useEffect(() => {
    loadDetail();
  }, [loadDetail]);

  // 2. SSE 订阅（仅当可能还在运行）
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      if (cancelled) return;
      // 已明确终结就不订阅（避免无谓连接）
      if (status === "completed" || status === "terminated" || status === "error") return;

      const clean = subscribeAdaptStream(
        sessionId,
        (evt) => handleEvent(evt),
        () => {
          // D10：断连不重连。标记异常。
          setStreamError("实时连接中断（进程可能重启）");
          setStatus((s) => (s === "loading" ? "error" : s));
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

  const handleEvent = (evt: AdaptStreamEvent) => {
    setStreamError(null);
    switch (evt.type) {
      case "session_hello":
        if (evt.terminal) {
          setStatus((evt.terminal as SessionStatus) || "terminated");
        } else {
          setStatus("running");
        }
        break;
      case "node_output":
        setCurrentNode(evt.node);
        setRound(evt.round);
        setOutputs((prev) => ({
          ...prev,
          [evt.node]: { payload: evt.payload, round: evt.round },
        }));
        setCompleted((prev) => {
          const next = new Set(prev);
          next.add(evt.node);
          return next;
        });
        if (evt.node === "critic") {
          setRevisionActive(evt.payload.critic_verdict?.verdict === "revision");
        }
        break;
      case "round_end":
        // 新轮开始：清空当前轮的节点完成态
        setCompleted(new Set());
        setRevisionActive(false);
        break;
      case "session_end":
        setStatus(evt.outcome);
        break;
      case "error":
        setStatus("error");
        setStreamError(evt.reason);
        break;
    }
  };

  const handleStop = async () => {
    setStopBusy(true);
    try {
      await stopSession(sessionId);
    } catch {
      // 409 = 已终结，忽略
    } finally {
      setStopBusy(false);
    }
  };

  if (!sessionId) {
    return (
      <div className="card">
        <div className="empty-state">
          <h3>缺少 session id</h3>
          <p>请从 <a href="/" className="text-dim" style={{ color: "var(--accent)" }}>进化总览</a> 选择一个 session</p>
        </div>
      </div>
    );
  }

  const isRunning = status === "running";
  const latestRound = detail?.rounds[detail.rounds.length - 1];

  return (
    <div className="cockpit">
      {/* 顶栏：session 元数据 + 状态 + 软停 */}
      <div className="cockpit-topbar">
        <div className="cockpit-title-block">
          <a href="/" className="cockpit-back mono text-mute">
            ← 总览
          </a>
          <h1 className="cockpit-title">
            Session <span className="mono">{sessionId}</span>
          </h1>
          <div className="cockpit-meta mono text-dim">
            基准 v{detail?.baseline_version ?? "—"}
            {detail && detail.rounds.length > 0 && <> · {detail.rounds.length} 轮留存</>}
          </div>
        </div>
        <div className="cockpit-topright">
          <SessionStatusBadge status={status === "loading" ? "running" : status} />
          {isRunning && (
            <button
              className="btn-ghost"
              onClick={handleStop}
              disabled={stopBusy}
              title="软停：当前轮跑完后结束（D12）"
            >
              {stopBusy ? "请求中…" : "软停"}
            </button>
          )}
        </div>
      </div>

      {streamError && (
        <div className="error-box" style={{ marginBottom: 16 }}>
          {streamError}。已落库的轮数据仍可查看。
        </div>
      )}

      {/* pipeline 流水线 */}
      <div className="card cockpit-pipeline-card">
        <AdaptPipeline
          current={currentNode}
          completed={completed}
          round={round}
          revisionActive={revisionActive}
        />
      </div>

      {/* 中部：节点产出区 */}
      <div className="cockpit-output">
        {currentNode && outputs[currentNode] ? (
          <NodeOutputView node={currentNode} payload={outputs[currentNode]!.payload} />
        ) : latestRound && !isRunning ? (
          <div className="node-pane">
            <div className="pane-header">
              <div>
                <div className="pane-title">最后一轮 landscape</div>
                <div className="pane-hint text-mute">round {latestRound.round} · 已结束</div>
              </div>
            </div>
            <div className="landscape-body prose-doc">
              <pre className="landscape-text">{latestRound.landscape || "（无 landscape）"}</pre>
            </div>
          </div>
        ) : (
          <div className="node-pane">
            <div className="empty-state">
              <p className="text-dim" style={{ fontSize: 13 }}>
                {isRunning ? "等待第一个节点产出…" : "无数据"}
              </p>
            </div>
          </div>
        )}
      </div>

      {/* 底部：历轮结果（已落库的轮）*/}
      {detail && detail.rounds.length > 0 && (
        <div className="cockpit-rounds">
          <div className="section-head">
            <h2 className="section-title">历轮结果</h2>
            <span className="text-mute mono" style={{ fontSize: 11 }}>
              已落库的轮（{detail.rounds.length}）
            </span>
          </div>
          <div className="card" style={{ padding: 0, overflow: "hidden" }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>轮</th>
                  <th>结果</th>
                  <th>发布版本</th>
                  <th>候选数</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {detail.rounds.map((r) => (
                  <RoundRow key={r.round} round={r} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function RoundRow({ round }: { round: AdaptRound }) {
  const shipped = round.round_outcome === "shipped";
  return (
    <tr>
      <td className="mono">round {round.round}</td>
      <td>
        <span
          className="status-badge"
          style={{
            color: shipped ? "var(--completed)" : "var(--cancelled)",
            background: shipped ? "rgba(63,185,80,.1)" : "rgba(139,148,158,.1)",
            border: `1px solid ${shipped ? "rgba(63,185,80,.25)" : "rgba(139,148,158,.2)"}`,
          }}
        >
          {round.round_outcome || "—"}
        </span>
      </td>
      <td className="mono">
        {round.shipped_version ? (
          <a href={`/versions/?v=${round.shipped_version}`} className="text-dim">
            v{round.shipped_version}
          </a>
        ) : (
          <span className="text-mute">—</span>
        )}
      </td>
      <td className="mono text-dim">{round.candidates.length}</td>
      <td className="mono text-mute">
        {round.created_at?.slice(0, 19).replace("T", " ")}
      </td>
    </tr>
  );
}
