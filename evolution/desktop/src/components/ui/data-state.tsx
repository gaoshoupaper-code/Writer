/**
 * 统一状态徽章 + 空/错/旧三态组件（FR-010 / FR-011 / AC-010）。
 *
 * StatusBadge：全端统一的状态语言（同一业务状态 → 同一中文标签/颜色/图标）。
 * EmptyState / ErrorState / StaleState：空数据、加载失败、陈旧缓存三态分离，
 * 不得把请求异常转为空数组后显示"暂无数据"（EDGE-007）。
 */

import { useEffect, useState } from "react";

// ── 统一状态徽章 ──────────────────────────────────────────────

export type StatusKind =
  | "running"
  | "cancelling"
  | "cancelled"
  | "cancel_timeout"
  | "completed"
  | "done"
  | "failed"
  | "interrupted"
  | "pending"
  | "ready"
  | "partial"
  | "sealed"
  | "verified"
  | "incomplete"
  | "legacy"
  | "conflict";

interface StatusBadgeProps {
  status: string | null | undefined;
  /** 可选自定义标签覆盖（默认用统一映射）。 */
  label?: string;
}

/** 统一状态标签映射（FR-010：消除"已停止"、英文 raw status 混用）。 */
const STATUS_LABELS: Record<string, string> = {
  running: "运行中",
  cancelling: "取消中",
  cancelled: "已取消",
  cancel_timeout: "取消超时",
  completed: "已完成",
  done: "已完成",
  failed: "失败",
  interrupted: "已中断",
  pending: "待处理",
  compiling: "编译中",
  ready: "就绪",
  partial: "部分",
  superseded: "已替代",
  sealed: "已封存",
  verified: "已验证",
  incomplete: "不完整",
  legacy: "旧版",
  conflict: "冲突",
};

/** 状态 → 语义色映射（FR-011：语义状态色可区分）。 */
const STATUS_SEMANTIC: Record<string, string> = {
  // 运行/进行
  running: "info",
  compiling: "info",
  pending: "info",
  // 取消
  cancelling: "warning",
  cancelled: "cancelled",
  cancel_timeout: "warning",
  interrupted: "cancelled",
  // 成功
  completed: "success",
  done: "success",
  ready: "success",
  sealed: "success",
  verified: "success",
  // 警告/部分
  partial: "warning",
  superseded: "cancelled",
  legacy: "cancelled",
  // 失败/错误
  failed: "danger",
  incomplete: "danger",
  conflict: "danger",
};

export function StatusBadge({ status, label }: StatusBadgeProps) {
  const raw = status ?? "unknown";
  const text = label ?? STATUS_LABELS[raw] ?? raw;
  const semantic = STATUS_SEMANTIC[raw] ?? "cancelled";
  return (
    <span
      className={`evo-status-badge evo-status-${semantic}`}
      role="status"
      aria-label={text}
    >
      {text}
    </span>
  );
}

// ── 空/错/旧三态分离（EDGE-007）──────────────────────────────

export function EmptyState({ message = "暂无数据" }: { message?: string }) {
  return (
    <div className="evo-state-empty" role="status">
      <p>{message}</p>
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="evo-state-error" role="alert">
      <p>加载失败：{message}</p>
      {onRetry && (
        <button type="button" className="evo-retry-button" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  );
}

export function StaleState({ lastSuccessAt }: { lastSuccessAt: string }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(id);
  }, []);
  const ageMs = now.getTime() - new Date(lastSuccessAt).getTime();
  const ageMin = Math.floor(ageMs / 60_000);
  return (
    <div className="evo-state-stale" role="status">
      <p>
        数据可能陈旧（最后成功：{ageMin > 0 ? `${ageMin} 分钟前` : "刚刚"}）
      </p>
    </div>
  );
}
