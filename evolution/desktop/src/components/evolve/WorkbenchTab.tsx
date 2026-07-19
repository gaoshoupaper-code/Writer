import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { evoSseStream } from "@/lib/stream";
import {
  finalizeEvolve,
  getEvolveMessages,
  getEvolvePoints,
  getEvolveSession,
  getEvolveSessions,
  getEvaluatedTraces,
  sendEvolveMessage,
  startEvolveConverse,
  stopEvolve,
  type EvalSession,
  type EvolveMessage,
  type EvolvePoint,
  type EvolveSession,
} from "@/lib/api";
import ConversationPanel from "./ConversationPanel";
import PointsDrawer from "./PointsDrawer";

/**
 * 进化工作台 Tab（决策 F/N/C）。
 *
 * 三栏布局：
 *   左：历史会话列表（点击切换）
 *   中：对话区（ConversationPanel）—— 启动入口 / 对话流 / 输入框
 *   右：进化点浮窗（PointsDrawer）—— 实时状态 + 拍板按钮
 *
 * 数据流：
 *   - 启动会话 → start-converse → 订阅 SSE → 拉取 messages + points
 *   - 用户发消息 → POST /messages → SSE 推 Agent 回复（持久化 + 增量拉取）
 *   - 进化点状态变更 → SSE proposal 事件 → 刷新 points
 *   - 用户拍板 → POST /finalize → finalizing → 完成后自动跳 review-report（决策 AA）
 *
 * 双向高亮联动（决策 N）：
 *   - 浮窗点击进化点 → highlightedPointId（滚动对话到该点讨论位置）
 *   - 对话区 hover/点击卡片 → 同一 state 反向高亮浮窗
 */
export default function WorkbenchTab() {
  const navigate = useNavigate();

  // 会话列表 + 评估trace列表（轮询）
  const [sessions, setSessions] = useState<EvolveSession[]>([]);
  const [evaluatedTraces, setEvaluatedTraces] = useState<EvalSession[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [selectedStatus, setSelectedStatus] = useState<string | null>(null);

  // 对话 + 进化点
  const [messages, setMessages] = useState<EvolveMessage[]>([]);
  const [points, setPoints] = useState<EvolvePoint[]>([]);
  const [acceptedCount, setAcceptedCount] = useState(0);

  // 交互态
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [highlightedPointId, setHighlightedPointId] = useState<string | null>(null);
  const streamCancelRef = useRef<(() => void) | null>(null);

  // ── 轮询：会话列表 + 评估trace 列表 ──────────────────────────
  const refreshLists = useCallback(async () => {
    const [sess, evals] = await Promise.all([
      getEvolveSessions(30).catch(() => null),
      getEvaluatedTraces(50).catch(() => null),
    ]);
    if (sess) setSessions(sess.sessions);
    if (evals) setEvaluatedTraces(evals.traces);
  }, []);

  useEffect(() => {
    void refreshLists();
    const timer = setInterval(refreshLists, 10000);
    return () => {
      clearInterval(timer);
      streamCancelRef.current?.();
    };
  }, [refreshLists]);

  // ── 拉取会话详情（messages + points）────────────────────────
  // 拉取进化点（独立于消息——proposal 事件时只刷进化点，避免覆盖流式 token）
  const loadPoints = useCallback(async (sessionId: string) => {
    try {
      const ptsResp = await getEvolvePoints(sessionId);
      if (ptsResp) {
        setPoints(ptsResp.points);
        setAcceptedCount(ptsResp.accepted_count);
      }
    } catch {
      setPoints([]);
      setAcceptedCount(0);
    }
  }, []);

  // 拉取消息（只在 phase 切换/选会话/SSE end 时调用——避免覆盖流式 token）
  const loadMessages = useCallback(async (sessionId: string) => {
    try {
      const msgResp = await getEvolveMessages(sessionId);
      if (msgResp) setMessages(msgResp.messages);
    } catch {
      setMessages([]);
    }
  }, []);

  const loadSessionDetail = useCallback(async (sessionId: string) => {
    // 选会话时同时拉消息 + 进化点（不涉及流式，安全）
    await Promise.all([loadMessages(sessionId), loadPoints(sessionId)]);
  }, [loadMessages, loadPoints]);

  // ── 选会话 ──────────────────────────────────────────────────
  function selectSession(s: EvolveSession) {
    streamCancelRef.current?.();
    setSelectedSessionId(s.session_id);
    setSelectedStatus(s.status);
    setHighlightedPointId(null);
    void loadSessionDetail(s.session_id);
    // 活跃会话订阅 SSE
    if (["running", "conversing", "finalizing"].includes(s.status)) {
      subscribeStream(s.session_id);
    }
  }

  // ── 启动新会话（对话式入口）─────────────────────────────────
  async function handleStart(traceId: string) {
    setStarting(true);
    setMessages([]);
    setPoints([]);
    setAcceptedCount(0);
    try {
      const resp = await startEvolveConverse(traceId);
      setSelectedSessionId(resp.session_id);
      setSelectedStatus("running");
      toast.success(`进化已启动：${resp.session_id.slice(0, 8)}`);
      subscribeStream(resp.session_id);
      void refreshLists();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "启动进化失败");
    } finally {
      setStarting(false);
    }
  }

  // ── SSE 订阅（决策 W：实时事件）─────────────────────────────
  async function subscribeStream(sessionId: string) {
    streamCancelRef.current?.();
    try {
      const gen = evoSseStream(`/api/evolve/sessions/${sessionId}/stream`, {
        method: "GET",
      });
      for await (const frame of gen) {
        handleSseFrame(sessionId, frame);
      }
    } catch {
      // SSE 断开（用户离开/网络问题），静默处理
    }
  }

  function handleSseFrame(sessionId: string, frame: any) {
    if (!frame || typeof frame !== "object") return;
    switch (frame.type) {
      case "heartbeat":
        break;
      case "model_stream": {
        // Phase 6 token 级流式：增量 token 拼接到当前 Agent 消息（打字机效果）
        const delta = frame.content;
        if (typeof delta !== "string" || !delta) break;
        setMessages((prev) => {
          // 找最后一条临时 assistant 消息（id 以 stream- 开头）
          const last = prev[prev.length - 1];
          if (last && last.role === "assistant" && last.id.startsWith("stream-")) {
            const updated = { ...last, content: last.content + delta };
            return [...prev.slice(0, -1), updated];
          }
          // 没有正在流的 assistant 消息，新建一条
          return [
            ...prev,
            {
              id: `stream-${Date.now()}`,
              session_id: sessionId,
              role: "assistant",
              content: delta,
              seq: prev.length + 1,
              created_at: new Date().toISOString(),
            },
          ];
        });
        break;
      }
      case "model_output": {
        // 一轮回复完整文本（含工具调用意图）——替换临时流式消息为持久版本
        const text = frame.text;
        if (typeof text !== "string") break;
        setMessages((prev) => {
          // 移除最后一条 stream- 消息（如果存在），追加完整消息
          const without = prev[prev.length - 1]?.id.startsWith("stream-")
            ? prev.slice(0, -1)
            : prev;
          if (!text) return without;
          return [
            ...without,
            {
              id: `asst-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
              session_id: sessionId,
              role: "assistant",
              content: text,
              seq: without.length + 1,
              created_at: new Date().toISOString(),
            },
          ];
        });
        break;
      }
      case "tool_call": {
        // 工具调用开始——注入为系统消息（不阻塞主流程）
        // 后端 sse_frame 包装时 tool → tool_name（避免与外层 tool 参数冲突）
        const tn = frame.tool_name || frame.tool;
        if (!tn) break;
        // 进化点工具的 tool_call 已被 sink 同时产 proposal 帧，不重复显示
        if (["propose_evolution_point", "update_evolution_point", "reject_evolution_point"].includes(tn)) {
          break;
        }
        setMessages((prev) => [
          ...prev,
          {
            id: `tool-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
            session_id: sessionId,
            role: "system",
            content: `[工具] ${tn}`,
            seq: prev.length + 1,
            created_at: new Date().toISOString(),
          },
        ]);
        break;
      }
      case "phase": {
        // 阶段切换（inspect → conversing → finalizing）
        setSelectedStatus(frame.phase);
        // 切到 conversing 时拉一次消息（Agent 开场白已落库）。
        // 注意：inspect round 跑完才会 conversing，此时不会与 token 流冲突。
        if (frame.phase === "conversing") {
          void loadMessages(sessionId);
          void loadPoints(sessionId);
        }
        break;
      }
      case "log":
      case "step": {
        // 探查/落地进度事件（决策 W/B）——注入为系统消息（临时，不入库）
        const text = frame.message || frame.tool || "";
        if (text) {
          setMessages((prev) => [
            ...prev,
            {
              id: `sys-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
              session_id: sessionId,
              role: "system",
              content: text,
              seq: prev.length + 1,
              created_at: new Date().toISOString(),
            },
          ]);
        }
        break;
      }
      case "proposal": {
        // 进化点状态变更 → 只刷浮窗（决策 B/M），不动消息避免覆盖流式 token
        void loadPoints(sessionId);
        break;
      }
      case "finalizing": {
        // 落地进度事件（决策 W）——注入为系统消息显示
        const evt = frame.event || "";
        const tgt = frame.target || "";
        const result = frame.result ? ` ${frame.result}` : "";
        const text = evt ? `[落地] ${evt} ${tgt}${result}`.trim() : "";
        if (text) {
          setMessages((prev) => [
            ...prev,
            {
              id: `fin-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
              session_id: sessionId,
              role: "system",
              content: text,
              seq: prev.length + 1,
              created_at: new Date().toISOString(),
            },
          ]);
        }
        break;
      }
      case "end": {
        // 流结束 → 刷新会话详情（拿最终 status）
        void refreshLists();
        void loadSessionDetail(sessionId);
        // 检查是否需要跳 review-report（pending_review 时，决策 AA）
        setTimeout(async () => {
          try {
            const sessResp = await getEvolveSession(sessionId);
            if (sessResp.status === "pending_review") {
              navigate(`/evolve/${sessionId}/review`);
            }
            setSelectedStatus(sessResp.status);
          } catch {
            // 静默
          }
        }, 500);
        break;
      }
      case "error": {
        toast.error("Agent 执行出错，请查看详情");
        void refreshLists();
        break;
      }
      default:
        // 其他事件类型（model_stream/tool_call 等）——Phase 4 不细处理，
        // Phase 5 接入 EvolveEventSink 后再细化渲染。
        break;
    }
  }

  // ── 发消息（决策 T2 按需触发）───────────────────────────────
  async function handleSend(content: string) {
    if (!selectedSessionId) return;
    // 乐观更新：先在前端追加用户消息
    const optimistic: EvolveMessage = {
      id: `tmp-${Date.now()}`,
      session_id: selectedSessionId,
      role: "user",
      content,
      seq: messages.length + 1,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, optimistic]);
    try {
      await sendEvolveMessage(selectedSessionId, content);
      // 真实消息后续通过 SSE / loadSessionDetail 同步
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "发送失败");
      // 失败回滚乐观更新
      setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
    }
  }

  // ── 停止（决策 L：只停输出，会话保留）───────────────────────
  async function handleStop() {
    if (!selectedSessionId) return;
    if (!window.confirm("确定停止 Agent 输出？会话保留，可继续输入。")) return;
    setStopping(true);
    try {
      await stopEvolve(selectedSessionId);
      streamCancelRef.current?.();
      toast.success("已停止");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "停止失败");
    } finally {
      setStopping(false);
    }
  }

  // ── 拍板（决策 C/D/T10）─────────────────────────────────────
  async function handleFinalize() {
    if (!selectedSessionId) return;
    if (acceptedCount === 0) {
      toast.error("至少需要采纳 1 个进化点才能拍板");
      return;
    }
    if (!window.confirm(`确认这 ${acceptedCount} 个进化点，开始落地？拍板后清单冻结。`))
      return;
    setFinalizing(true);
    try {
      await finalizeEvolve(selectedSessionId);
      setSelectedStatus("finalizing");
      toast.success("已拍板，Agent 开始落地…");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "拍板失败");
    } finally {
      setFinalizing(false);
    }
  }

  const canFinalize = selectedStatus === "conversing" && acceptedCount >= 1;

  return (
    <div className="evolve-workbench">
      {/* 左：会话列表 */}
      <aside className="workbench-sidebar">
        <h3 className="sidebar-title">进化历史</h3>
        <ul className="session-list">
          {sessions.length === 0 ? (
            <li className="session-empty">暂无进化记录</li>
          ) : (
            sessions.map((s) => {
              const isActive = s.session_id === selectedSessionId;
              return (
                <li
                  key={s.session_id}
                  className={`session-item${isActive ? " active" : ""}`}
                  onClick={() => selectSession(s)}
                >
                  <div className="session-item-head">
                    <span className={`session-status status-${s.status}`}>
                      {STATUS_DOT[s.status] ?? "?"}
                    </span>
                    <code className="session-id">{s.session_id.slice(0, 8)}</code>
                  </div>
                  <time className="session-time">{formatTime(s.created_at)}</time>
                </li>
              );
            })
          )}
        </ul>
      </aside>

      {/* 中：对话区 */}
      <ConversationPanel
        selectedSessionId={selectedSessionId}
        status={selectedStatus}
        messages={messages}
        points={points}
        evaluatedTraces={evaluatedTraces}
        starting={starting}
        stopping={stopping}
        highlightedPointId={highlightedPointId}
        onStart={handleStart}
        onSend={handleSend}
        onStop={handleStop}
        onPointHover={setHighlightedPointId}
      />

      {/* 右：进化点浮窗 */}
      <PointsDrawer
        points={points}
        acceptedCount={acceptedCount}
        canFinalize={canFinalize}
        finalizing={finalizing}
        highlightedPointId={highlightedPointId}
        onPointClick={(id) => setHighlightedPointId(id)}
        onFinalize={handleFinalize}
      />
    </div>
  );
}

const STATUS_DOT: Record<string, string> = {
  running: "●",
  conversing: "●",
  finalizing: "●",
  pending_review: "◆",
  published: "✓",
  discarded: "✗",
  failed: "!",
  cancelled: "○",
};

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  } catch {
    return iso;
  }
}
