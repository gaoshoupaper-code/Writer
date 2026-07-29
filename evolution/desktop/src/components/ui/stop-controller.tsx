/**
 * StopController —— 统一停止反馈组件（FR-008 / AC-008 / DEC-002）。
 *
 * 抽取 5 个页面（tests / trace-detail / dossiers / evaluation / evolve）各自的停止
 * 逻辑为单一组件，保证全端一致的取消状态语言和交互：
 *   - 提交即禁用，显示"取消中"（DEC-002 立即反馈）
 *   - 持续轮询到权威终态（不在最需要确认终态时关闭轮询）
 *   - 统一语言："取消中"/"已取消"/"取消超时"，消除"已停止"、英文 raw status
 *   - 旧响应不覆盖新状态（EDGE-003）
 */

import { useState, useCallback } from "react";

export type CancelStatus =
  | "idle"          // 未停止
  | "cancelling"   // 已提交停止，等待收敛（DEC-002 立即可见中间态）
  | "cancelled"    // 确认收敛终态
  | "cancel_timeout"; // 10s 内未收敛，诚实告警态（EDGE-004）

export interface StopControllerProps {
  /** 当前对象是否处于可停止状态（running/cancelling）。 */
  canStop: boolean;
  /** 执行停止的 API 调用（返回后进入 cancelling）。 */
  onStop: () => Promise<void>;
  /** 当前对象的服务端终态（轮询回填），用于从 cancelling 收敛到终态。 */
  serverStatus?: string | null;
  /** 可选：停止失败时的错误回调。 */
  onError?: (error: string) => void;
  /** 可选：自定义按钮 className。 */
  className?: string;
}

/** 统一状态语言（FR-008：消除"已停止"、英文 raw status）。 */
export function cancelStatusLabel(status: CancelStatus | string | null | undefined): string {
  if (status === "cancelling") return "取消中";
  if (status === "cancelled") return "已取消";
  if (status === "cancel_timeout") return "取消超时";
  if (status === "running" || status === "idle") return "运行中";
  return "—";
}

export function StopController({
  canStop,
  onStop,
  serverStatus,
  onError,
  className = "stop-button",
}: StopControllerProps) {
  const [cancelStatus, setCancelStatus] = useState<CancelStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  // serverStatus 从 cancelling 收敛到终态时，同步本地状态。
  const effectiveStatus: CancelStatus = (() => {
    if (cancelStatus === "cancelling") {
      // 权威终态回填：cancelled / cancel_timeout / 其他终态。
      if (serverStatus === "cancelled") return "cancelled";
      if (serverStatus === "cancel_timeout") return "cancel_timeout";
      // 其他终态（done/failed）也意味着取消流程结束（竞态：先完成）。
      if (serverStatus && serverStatus !== "running" && serverStatus !== "cancelling") {
        return cancelStatus; // 保持 cancelling，让调用方据 serverStatus 判断
      }
      return "cancelling";
    }
    return cancelStatus;
  })();

  const handleStop = useCallback(async () => {
    if (cancelStatus !== "idle") return; // 幂等：重复点击不重复提交（EDGE-003）
    setCancelStatus("cancelling");
    setError(null);
    try {
      await onStop();
      // onStop 返回后保持 cancelling——终态由 serverStatus 轮询回填。
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
      setCancelStatus("idle"); // 失败回到 idle，允许重试（EDGE-003）
      onError?.(msg);
    }
  }, [cancelStatus, onStop, onError]);

  const isCancelling = effectiveStatus === "cancelling";
  const isTerminal = effectiveStatus === "cancelled" || effectiveStatus === "cancel_timeout";

  // 按钮文案统一（FR-008）。
  const label = isCancelling ? "取消中…" : isTerminal ? cancelStatusLabel(effectiveStatus) : "停止";

  return (
    <div className="stop-controller">
      <button
        type="button"
        className={className}
        disabled={!canStop || isCancelling}
        onClick={handleStop}
        aria-label="停止运行中的任务"
      >
        {label}
      </button>
      {error && <span className="stop-error">{error}</span>}
      {effectiveStatus === "cancel_timeout" && (
        <span className="stop-warning">
          取消超时：任务可能仍在后台运行，可再次尝试强制停止。
        </span>
      )}
    </div>
  );
}
