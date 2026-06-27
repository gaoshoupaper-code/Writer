// StatusBadge —— trace 5 态状态徽章（视觉规范集中管理）
//
// 5 态对应 trace 生命周期：running / awaiting_input / completed / failed / cancelled
// running 带脉冲圆点（唯一允许的装饰性动效，功能性："还活着"）

import type { TraceStatus } from "@/lib/types";

const STATUS_CONFIG: Record<string, { label: string; color: string; bg: string; border: string; pulse: boolean }> = {
  running: { label: "Running", color: "var(--running)", bg: "rgba(88,166,255,.12)", border: "rgba(88,166,255,.25)", pulse: true },
  awaiting_input: { label: "Awaiting", color: "var(--awaiting)", bg: "rgba(227,179,65,.12)", border: "rgba(227,179,65,.25)", pulse: false },
  completed: { label: "Completed", color: "var(--completed)", bg: "rgba(63,185,80,.12)", border: "rgba(63,185,80,.25)", pulse: false },
  failed: { label: "Failed", color: "var(--failed)", bg: "rgba(248,81,73,.12)", border: "rgba(248,81,73,.25)", pulse: false },
  cancelled: { label: "Cancelled", color: "var(--cancelled)", bg: "rgba(139,148,158,.12)", border: "rgba(139,148,158,.2)", pulse: false },
};

export function StatusBadge({ status }: { status: TraceStatus | string }) {
  const cfg = STATUS_CONFIG[status] ?? STATUS_CONFIG.running;
  return (
    <span
      className="status-badge"
      style={{
        color: cfg.color,
        background: cfg.bg,
        border: `1px solid ${cfg.border}`,
      }}
    >
      {cfg.pulse ? (
        <span
          className="pulse-dot"
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: cfg.color,
            flexShrink: 0,
          }}
        />
      ) : null}
      {cfg.label}
    </span>
  );
}

export function statusLabel(status: TraceStatus | string): string {
  return STATUS_CONFIG[status]?.label ?? status;
}
