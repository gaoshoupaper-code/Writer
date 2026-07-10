/**
 * FailedView —— 失败态·拟人化歉意+重试
 *
 * 触发：error → message.status: failed → phase: failed。
 * 错误原因翻译成人话（HEARTBEAT_TIMEOUT→"连接好像断了"等）。
 */
import type { ChatMessage } from "@/lib/types";
import { getFailedCopy, FAILED_ACTION } from "@/lib/yan-copy";

interface FailedViewProps {
  message: ChatMessage;
  onRetry?: () => void;
}

export function FailedView({ message, onRetry }: FailedViewProps) {
  const copy = getFailedCopy(message.content);

  return (
    <div className="yan-failed" data-phase="failed">
      <p className="yan-failed-copy">{copy}</p>
      <p className="yan-failed-action">{FAILED_ACTION}</p>
      <button type="button" className="yan-retry-button" onClick={() => onRetry?.()}>
        ↻ 再试一次
      </button>
    </div>
  );
}
