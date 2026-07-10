/**
 * ExecutionView —— 小衍执行体验容器
 *
 * 按 executionPhase 切换子视图，取代 StageFlowView 在对话流中的角色。
 * 嵌入 assistant message 内，渲染执行过程的"有温度"反馈。
 *
 * T17 正式版：phase 优先读 message.executionPhase（由 executionStore
 * 在每次 message 更新后通过 derivePhaseFromMessage 写入）。
 * 兜底：若 executionPhase 缺失（如历史消息未被 store 处理），临时派生。
 *
 * 历史消息（非活跃 trace）显示折叠态（delivered + 可展开执行过程摘要）。
 */
import type { ChatMessage, ExecutionPhase } from "@/lib/types";
import type { StageFlow } from "@/lib/stage";
import { derivePhaseFromMessage } from "@/lib/execution-phase";
import { BootingView } from "./execution/BootingView";
import { ThinkingView } from "./execution/ThinkingView";
import { WritingProgress } from "./execution/WritingProgress";
import { DeliveryCeremony } from "./execution/DeliveryCeremony";
import { FailedView } from "./execution/FailedView";
import { StoppedView } from "./execution/StoppedView";
import { HistoryFolded } from "./execution/HistoryFolded";

interface ExecutionViewProps {
  message: ChatMessage;
  loading: boolean;
  isLastAssistant: boolean; // 是否为最后一条 assistant（活跃执行中）
  stageFlow: StageFlow | null;
  onRetry?: () => void;
}

export function ExecutionView({ message, loading, isLastAssistant, stageFlow, onRetry }: ExecutionViewProps) {
  // T17: 优先用 message.executionPhase（store 写入），兜底用临时派生
  const phase: ExecutionPhase = message.executionPhase ?? derivePhaseFromMessage(message);

  // 历史消息（非活跃）：显示折叠态
  if (!isLastAssistant && (phase === "delivering" || phase === "idle")) {
    return <HistoryFolded stageFlow={stageFlow} />;
  }

  switch (phase) {
    case "booting":
      return <BootingView />;
    case "thinking":
      return <ThinkingView message={message} stageFlow={stageFlow} />;
    case "writing":
      return <WritingProgress message={message} stageFlow={stageFlow} />;
    case "asking":
      // asking 态的 HITL UI 由 ChatPanel 外层渲染（InterviewOptions/ImageReviewCard）
      // ExecutionView 只负责执行过程反馈，asking 时不渲染额外内容
      return null;
    case "delivering":
      return <DeliveryCeremony message={message} stageFlow={stageFlow} />;
    case "failed":
      return <FailedView message={message} onRetry={onRetry} />;
    case "stopped":
      return <StoppedView message={message} onRetry={onRetry} />;
    default:
      return null;
  }
}
