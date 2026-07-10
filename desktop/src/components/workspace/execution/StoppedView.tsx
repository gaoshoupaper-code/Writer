/**
 * StoppedView —— 停止态·拟人化+继续
 *
 * 触发：用户主动停止 → message.status: stopped → phase: stopped。
 */
import type { ChatMessage } from "@/lib/types";
import { STOPPED_COPY, STOPPED_ACTION } from "@/lib/yan-copy";

interface StoppedViewProps {
  message: ChatMessage;
  onRetry?: () => void;
}

export function StoppedView({ onRetry }: StoppedViewProps) {
  return (
    <div className="yan-stopped" data-phase="stopped">
      <p className="yan-stopped-copy">{STOPPED_COPY}</p>
      <p className="yan-stopped-action">{STOPPED_ACTION}</p>
      <button type="button" className="yan-retry-button" onClick={() => onRetry?.()}>
        ↻ 继续
      </button>
    </div>
  );
}
