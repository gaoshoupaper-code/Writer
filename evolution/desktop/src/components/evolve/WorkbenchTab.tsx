import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  finalizeEvolve,
  getEvolveMessages,
  getEvolvePoints,
  getEvolveSession,
  getEvaluatedTraces,
  getEvolveSessionEventsSince,
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
 * 进化工作台 Tab（决策 F/N/C，2026-07-20 重构为两栏）。
 *
 * 两栏布局：
 *   中：对话区（ConversationPanel）—— 启动入口 / 对话流 / 输入框
 *   右：进化点浮窗（PointsDrawer）—— 实时状态 + 拍板按钮
 * （原左侧历史会话已移到独立「进化历史」Tab，本组件不再维护 sessions 列表）
 *
 * 跨 tab 选中联动（DD4）：
 *   - initialSessionId：URL ?session=xxx 解析出的 id（EvolvePage 透传）
 *   - initialSession：HistoryTab 点选时透传的完整 session 对象（含 status，免重复拉详情）
 *   - useEffect([initialSessionId])：id 变化时自动选中（有 initialSession 直接用，否则按 id 拉详情）
 *
 * 数据流：
 *   - 启动会话 → start-converse → 订阅 Pull 事件流 → 拉取 messages + points
 *   - 用户发消息 → POST /messages → Pull 推 Agent 回复（持久化 + 增量拉取）
 *   - 进化点状态变更 → proposal 事件 → 刷新 points
 *   - 用户拍板 → POST /finalize → finalizing → 完成后自动跳 review-report（决策 AA）
 *
 * 双向高亮联动（决策 N）：
 *   - 浮窗点击进化点 → highlightedPointId（滚动对话到该点讨论位置）
 *   - 对话区 hover/点击卡片 → 同一 state 反向高亮浮窗
 */
export default function WorkbenchTab({
  initialSessionId,
  initialSession,
}: {
  initialSessionId: string | null;
  initialSession: EvolveSession | null;
}) {
  const navigate = useNavigate();

  // 评估trace列表（轮询，启动入口用）——sessions 列表已迁到 HistoryTab
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

  // ── 轮询：评估trace 列表（启动入口用）──────────────────────
  // sessions 列表已迁到 HistoryTab，本组件只保留 evaluatedTraces 轮询。
  const refreshEvaluatedTraces = useCallback(async () => {
    const evals = await getEvaluatedTraces(50).catch(() => null);
    if (evals) setEvaluatedTraces(evals.traces);
  }, []);

  useEffect(() => {
    void refreshEvaluatedTraces();
    const timer = setInterval(refreshEvaluatedTraces, 10000);
    return () => {
      clearInterval(timer);
      streamCancelRef.current?.();
    };
  }, [refreshEvaluatedTraces]);

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

  // ── 选中会话（核心动作，供 initialSessionId 联动 + handleStart 复用）──
  // 依赖只列 loadSessionDetail——subscribeStream 是 hoisted 函数声明，每次 render
  // 重建，若列进依赖会让 selectSession 每次 render 都变，破坏 useEffect 幂等性。
  // subscribeStream 内部用 setState 函数式更新 + ref，闭包稳定性已足够。
  const selectSession = useCallback(
    (s: EvolveSession) => {
      streamCancelRef.current?.();
      setSelectedSessionId(s.session_id);
      setSelectedStatus(s.status);
      setHighlightedPointId(null);
      void loadSessionDetail(s.session_id);
      // 活跃会话订阅 Pull 事件流
      if (["running", "conversing", "finalizing"].includes(s.status)) {
        subscribeStream(s.session_id);
      }
    },
    [loadSessionDetail],
  );

  // ── URL ?session=xxx 联动（DD4）─────────────────────────────
  // HistoryTab 点选 → EvolvePage 写 URL → 本 effect 触发选中。
  // 有 initialSession 对象直接用（免拉详情）；只有 id（刷新场景）时按 id 拉详情。
  useEffect(() => {
    if (!initialSessionId) return;
    // 已选中相同 session 则跳过（幂等，避免重复订阅）
    if (initialSessionId === selectedSessionId) return;

    if (initialSession && initialSession.session_id === initialSessionId) {
      selectSession(initialSession);
    } else {
      // 刷新场景：URL 有 id 但无 session 对象 → 拉详情后选中
      void getEvolveSession(initialSessionId)
        .then((sess) => selectSession(sess))
        .catch(() => {
          // session 不存在或拉取失败：静默，中栏保持启动入口态
        });
    }
  }, [initialSessionId, initialSession, selectSession, selectedSessionId]);

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
      void refreshEvaluatedTraces();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "启动进化失败");
    } finally {
      setStarting(false);
    }
  }

  // ── trace 重构：Pull 轮询（设计 20260720_154825）──
  // 重构后事件密度大幅降低（不再有 token 流），轮询间隔放宽到 2s。
  // 前端不再维护临时消息 state——所有消息（assistant/tool/system）都从
  // evolve_messages 权威存储拉取，事件帧只是"通知该刷消息了"的信号。
  async function subscribeStream(sessionId: string) {
    streamCancelRef.current?.();
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let sinceSeq = 0;

    streamCancelRef.current = () => {
      cancelled = true;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    };

    const POLL_INTERVAL_MS = 2000;  // 重构后无 token 流，2s 足够
    const ERROR_BACKOFF_MS = 3000;

    const poll = async () => {
      if (cancelled) return;
      try {
        const resp = await getEvolveSessionEventsSince(sessionId, sinceSeq);
        if (cancelled) return;

        // 派发每帧到 handleSseFrame
        for (const frame of resp.frames) {
          handleSseFrame(sessionId, frame);
        }
        sinceSeq = resp.max_seq;

        // has_more=true：事件积压（罕见，重构后事件密度低），立即续拉。
        if (resp.has_more) {
          timer = setTimeout(poll, 0);
          return;
        }

        // session_status 终态：派发 end 帧，然后停止轮询。
        const terminal = ["published", "discarded", "failed", "cancelled"].includes(
          resp.session_status,
        );
        if (terminal) {
          handleSseFrame(sessionId, { type: "end" });
          return;
        }

        // running 中：安排下次轮询。
        timer = setTimeout(poll, POLL_INTERVAL_MS);
      } catch {
        if (cancelled) return;
        // 网络抖动：退避后继续（不丢已拉帧）。
        timer = setTimeout(poll, ERROR_BACKOFF_MS);
      }
    };

    poll();
  }

  function handleSseFrame(sessionId: string, frame: any) {
    if (!frame || typeof frame !== "object") return;
    switch (frame.type) {
      case "heartbeat":
        break;
      case "message_updated": {
        // Agent 落了一条新消息（assistant/tool）到 evolve_messages。
        // 拉权威存储——前端不维护临时消息，刷新即拿到最新内容。
        void loadMessages(sessionId);
        break;
      }
      case "phase": {
        // 阶段切换（inspect → conversing → finalizing）
        setSelectedStatus(frame.phase);
        // 切到 conversing 时拉消息（Agent 开场白已落库）+ 进化点。
        if (frame.phase === "conversing") {
          void loadMessages(sessionId);
          void loadPoints(sessionId);
        }
        break;
      }
      case "proposal": {
        // 进化点状态变更 → 刷浮窗（决策 B/M）+ 刷消息（tool 消息已落库）
        void loadPoints(sessionId);
        void loadMessages(sessionId);
        break;
      }
      case "finalizing": {
        // 落地进度事件——后端已把 tool 消息落库，刷消息即可
        void loadMessages(sessionId);
        break;
      }
      case "log": {
        // 思考日志事件——后端 emit_log 写的 run_meta，未落消息表。
        // 这里不展示（避免与持久化消息重复），日志可去 trace 详情页看。
        break;
      }
      case "step": {
        // 业务步骤事件（read_eval_report 等）——同 log，不展示在对话区。
        break;
      }
      case "end": {
        // 流结束 → 刷新会话详情（拿最终 status）
        void refreshEvaluatedTraces();
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
        void refreshEvaluatedTraces();
        break;
      }
      default:
        // 未知事件类型——重构后只有上面几种，安全忽略。
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
      {/* 中：对话区（原左侧历史已移到独立「进化历史」Tab）*/}
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
