// SessionStatusBadge —— adapt session 4 态状态徽章。
//
// running（进行中，带脉冲）/ completed（正常结束）/ terminated（被软停或丢失）/ error（异常）
// 复用 trace StatusBadge 的视觉模式（status-badge 基类），语义色对齐。

import type { SessionStatus } from "@/lib/adapt-types";

const STATUS_CONFIG: Record<
  SessionStatus,
  { label: string; color: string; bg: string; border: string; pulse: boolean }
> = {
  running: {
    label: "Running",
    color: "var(--running)",
    bg: "rgba(88,166,255,.12)",
    border: "rgba(88,166,255,.25)",
    pulse: true,
  },
  completed: {
    label: "Done",
    color: "var(--completed)",
    bg: "rgba(63,185,80,.12)",
    border: "rgba(63,185,80,.25)",
    pulse: false,
  },
  terminated: {
    label: "Stopped",
    color: "var(--cancelled)",
    bg: "rgba(139,148,158,.12)",
    border: "rgba(139,148,158,.2)",
    pulse: false,
  },
  error: {
    label: "Error",
    color: "var(--failed)",
    bg: "rgba(248,81,73,.12)",
    border: "rgba(248,81,73,.25)",
    pulse: false,
  },
};

export function SessionStatusBadge({ status }: { status: SessionStatus | string }) {
  const cfg = STATUS_CONFIG[status as SessionStatus] ?? STATUS_CONFIG.running;
  return (
    <span
      className="status-badge"
      style={{ color: cfg.color, background: cfg.bg, border: `1px solid ${cfg.border}` }}
    >
      {cfg.pulse ? (
        <span
          className="pulse-dot"
          style={{ width: 6, height: 6, borderRadius: "50%", background: cfg.color, flexShrink: 0 }}
        />
      ) : null}
      {cfg.label}
    </span>
  );
}
