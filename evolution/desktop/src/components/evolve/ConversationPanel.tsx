import { useEffect, useRef, useState } from "react";
import type { EvalSession, EvolveMessage, EvolvePoint } from "@/lib/api";
import EvolveMessageBubble from "./EvolveMessageBubble";

/**
 * 中部对话区（决策 J/K/L/X）。
 *
 * 三种视图状态：
 *   - idle（无选中会话）：显示启动入口（选 trace + 启动按钮）
 *   - conversing / running / finalizing：显示对话流 + 输入框（conversing 可输入）
 *   - terminal（published/discarded/failed/cancelled）：只读对话流
 *
 * 输入框（决策 X）：纯文本 + Enter 发送，Shift+Enter 换行。conversing 状态可输入，
 * 其他状态禁用（finalizing 等待落地完成）。
 *
 * Agent 主动开场（决策 J）：inspect round 跑完后会有一条 assistant 消息。
 *
 * 双向高亮联动（决策 N）：
 *   - 点击浮窗进化点 → 滚动到本区对应消息（onScrollToMessage 触发）
 *   - hover 消息里的进化点卡片 → 触发 onPointHover（浮窗高亮该项）
 */
interface Props {
  selectedSessionId: string | null;
  status: string | null;
  messages: EvolveMessage[];
  points: EvolvePoint[];
  evaluatedTraces: EvalSession[];
  starting: boolean;
  stopping: boolean;
  highlightedPointId: string | null; // 来自浮窗点击
  onStart: (traceId: string) => void;
  onSend: (content: string) => void;
  onStop: () => void;
  onPointHover: (pointId: string | null) => void;
}

export default function ConversationPanel({
  selectedSessionId,
  status,
  messages,
  points,
  evaluatedTraces,
  starting,
  stopping,
  highlightedPointId,
  onStart,
  onSend,
  onStop,
  onPointHover,
}: Props) {
  const [selectedTraceId, setSelectedTraceId] = useState("");
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messageRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  // 自动滚到底（新消息到达时）
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  // 浮窗点击进化点 → 滚动到对应消息（决策 N 双向联动）
  useEffect(() => {
    if (!highlightedPointId) return;
    // 找到 related_points 含该 id 的消息
    const targetMsg = messages.find(
      (m) => m.related_points && m.related_points.includes(highlightedPointId),
    );
    if (targetMsg) {
      const el = messageRefs.current.get(targetMsg.id);
      el?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    // 3 秒后自动清高亮（参考 TraceChainTimeline 范式）
    const timer = setTimeout(() => onPointHover(null), 3000);
    return () => clearTimeout(timer);
  }, [highlightedPointId, messages, onPointHover]);

  // ── idle 视图：启动入口 ────────────────────────────────────
  if (!selectedSessionId) {
    return (
      <section className="conversation-panel idle">
        <div className="start-card">
          <div className="start-icon">🧬</div>
          <h2 className="start-title">启动一次进化共创</h2>
          <p className="start-subtitle">
            选一条已评估的 trace，进化 Agent 会先读评估报告 + 探查要素，
            然后和你一起讨论怎么改。
          </p>
          <div className="start-form">
            <select
              className="trace-select"
              value={selectedTraceId}
              onChange={(e) => setSelectedTraceId(e.target.value)}
              disabled={starting || evaluatedTraces.length === 0}
            >
              <option value="">
                {evaluatedTraces.length === 0
                  ? "暂无已评估的 trace"
                  : "选择一条已评估的 trace…"}
              </option>
              {evaluatedTraces.map((t) => (
                <option key={t.eval_id} value={t.trace_id}>
                  {t.trace_id?.slice(0, 12)} · {t.status || "done"}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="start-btn"
              disabled={!selectedTraceId || starting}
              onClick={() => selectedTraceId && onStart(selectedTraceId)}
            >
              {starting ? "启动中…" : "启动进化"}
            </button>
          </div>
        </div>
      </section>
    );
  }

  // ── 对话视图 ────────────────────────────────────────────────
  const isConversing = status === "conversing";
  const isRunning = status === "running" || status === "finalizing";
  const isTerminal =
    status === "published" ||
    status === "discarded" ||
    status === "failed" ||
    status === "cancelled";
  const canInput = isConversing;

  async function handleSend() {
    const content = input.trim();
    if (!content || !canInput) return;
    setSending(true);
    setInput("");
    try {
      await onSend(content);
    } finally {
      setSending(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSend();
    }
  }

  const statusBadge = STATUS_LABEL[status ?? ""] ?? status;

  return (
    <section className="conversation-panel">
      <header className="conversation-header">
        <div className="conv-status">
          <span className={`status-dot status-${status}`} />
          <span className="status-text">{statusBadge}</span>
          <code className="session-id">{selectedSessionId.slice(0, 8)}</code>
        </div>
        {(isRunning || isConversing) && (
          <button
            type="button"
            className="stop-btn"
            onClick={onStop}
            disabled={stopping}
          >
            {stopping ? "停止中…" : "停止"}
          </button>
        )}
      </header>

      <div className="message-list">
        {messages.length === 0 ? (
          <div className="conv-empty">
            <div className="empty-glyph">⏳</div>
            <p className="empty-text">
              {isRunning
                ? "Agent 正在探查评估报告和 harness 要素，请稍候…"
                : "等待 Agent 发出开场白…"}
            </p>
          </div>
        ) : (
          <>
            {messages.map((msg) => (
              <EvolveMessageBubble
                key={msg.id}
                ref={(el: HTMLDivElement | null) => {
                  if (el) messageRefs.current.set(msg.id, el);
                  else messageRefs.current.delete(msg.id);
                }}
                message={msg}
                points={points}
                highlightedPointId={highlightedPointId}
                onPointClick={onPointHover}
              />
            ))}
            <div ref={messagesEndRef} />
          </>
        )}
      </div>

      <footer className="composer">
        <textarea
          className="composer-input"
          placeholder={
            canInput
              ? "和 Agent 讨论改进点…（Enter 发送，Shift+Enter 换行）"
              : isTerminal
                ? "会话已结束"
                : "Agent 正在工作，请稍候…"
          }
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={!canInput}
          rows={2}
        />
        <button
          type="button"
          className="send-btn"
          onClick={handleSend}
          disabled={!canInput || sending || !input.trim()}
        >
          {sending ? "发送中…" : "发送"}
        </button>
      </footer>
    </section>
  );
}

const STATUS_LABEL: Record<string, string> = {
  running: "探查中",
  conversing: "对话共创中",
  finalizing: "落地中",
  pending_review: "待审查",
  published: "已发版",
  discarded: "已丢弃",
  failed: "失败",
  cancelled: "已取消",
};
