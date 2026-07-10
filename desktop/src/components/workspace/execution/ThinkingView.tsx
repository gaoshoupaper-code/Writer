/**
 * ThinkingView —— 思考流·默认折叠摘要
 *
 * 触发：收到 trace_event（run_start）且未进入 writing 阶段。
 *
 * 折叠态（默认）：拟人化摘要一行 + 脉动圆点。
 * 展开态：显示真实 reasoning 文本（来自 activeReasoning，P2 reasoning_stream）。
 *         如果 activeReasoning 为空（非 deepseek 模型或尚未产出），显示占位提示。
 */
import { useState } from "react";
import type { ChatMessage } from "@/lib/types";
import type { StageFlow } from "@/lib/stage";
import { getThinkingCopy, getStageDisplayName } from "@/lib/yan-copy";
import { useExecutionStore } from "@/stores/execution";

interface ThinkingViewProps {
  message: ChatMessage;
  stageFlow: StageFlow | null;
}

export function ThinkingView({ message, stageFlow }: ThinkingViewProps) {
  const [expanded, setExpanded] = useState(false);
  // T22: 从 executionStore 读瞬态 reasoning（不持久化）
  const activeReasoning = useExecutionStore((s) => s.activeReasoning);

  // 从 stageFlow 推断当前阶段 type
  const currentStage = stageFlow?.stages.find((s) => s.status === "running");
  const stageType = currentStage?.type ?? message.tools?.find((t) => t.status === "running")?.subagentType;
  const stageName = getStageDisplayName(stageType);
  const summaryText = getThinkingCopy(stageType);

  return (
    <div className="yan-thinking" data-phase="thinking">
      <button
        type="button"
        className="yan-thinking-summary"
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="yan-status-dot" data-status="running" />
        <span className="yan-thinking-text">{summaryText}</span>
        <span className="yan-thinking-stage">{stageName}</span>
        {activeReasoning ? <span className="yan-thinking-expand-hint">{expanded ? "▴" : "▾"}</span> : null}
      </button>

      {expanded ? (
        <div className="yan-thinking-detail">
          {activeReasoning ? (
            <pre className="yan-thinking-reasoning">{activeReasoning}</pre>
          ) : (
            <p className="yan-thinking-placeholder">
              小衍正在{stageName}，详细思考过程稍后会上线～
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
}
