/**
 * executionPhase 派生逻辑（T17）
 *
 * 从 ChatMessage 的 status/tools/awaitingInput 派生 ExecutionPhase。
 * 被 executionStore（每次 message 更新后调用）和 ExecutionView（兜底）共用。
 *
 * 派生规则（优先级从高到低）：
 *   awaitingInput 存在    → asking
 *   status === completed  → delivering
 *   status === failed     → failed
 *   status === stopped    → stopped
 *   有 writing tool running → writing
 *   有任何 tool running    → thinking（涵盖 storybuilding/detail-outline/general）
 *   content 为空/占位     → booting（黑屏期）
 *   有流式正文但无 tool    → thinking
 *   其他                   → idle
 */
import type { ChatMessage, ExecutionPhase } from "./types";

export function derivePhaseFromMessage(message: ChatMessage): ExecutionPhase {
  if (message.awaitingInput) return "asking";
  if (message.status === "completed") return "delivering";
  if (message.status === "failed") return "failed";
  if (message.status === "stopped") return "stopped";

  // 有 writing subagent running → writing
  const hasWriting = message.tools?.some(
    (t) => t.status === "running" && t.subagentType === "writing",
  );
  if (hasWriting) return "writing";

  // 有任何 tool running → thinking
  const hasRunning = message.tools?.some((t) => t.status === "running");
  if (hasRunning) return "thinking";

  // content 为空或占位 → booting（黑屏期，loading 中但还没收到事件）
  if (!message.content || message.content === "正在执行..." || message.content === "正在生成..." || message.content === "正在优化...") {
    return "booting";
  }

  // 有流式正文但无 running tool → thinking（可能是 model_stream 产出阶段）
  if (message.content) return "thinking";

  return "idle";
}
