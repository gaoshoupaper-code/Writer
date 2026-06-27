"use client";

/**
 * RewardChart —— 纯 SVG reward 趋势线（无图表库）。
 *
 * 设计取舍：
 *   - 不用 recharts/charism（项目没装，且通用图表太"报表感"）。
 *   - 手绘紧凑折线 + 端点高亮 + hover tooltip，贴合 dark technical 质感。
 *   - 横轴=版本号（等距，因为版本不一定均匀），纵轴=reward。
 *   - 单 accent 色，网格用极淡的 border-soft。
 *
 * 数据少（<2 点）时退化为单点显示，不画线。
 */
import { useState } from "react";

export type RewardPoint = { version: number; reward: number };

export function RewardChart({ points }: { points: RewardPoint[] }) {
  const [hover, setHover] = useState<number | null>(null);

  if (points.length === 0) return null;

  // 布局参数
  const W = 880;
  const H = 200;
  const PAD_L = 48;
  const PAD_R = 24;
  const PAD_T = 20;
  const PAD_B = 34;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;

  // reward 范围（带 padding，避免贴边）
  const rewards = points.map((p) => p.reward);
  let yMin = Math.min(...rewards);
  let yMax = Math.max(...rewards);
  if (yMin === yMax) {
    yMin -= 0.05;
    yMax += 0.05;
  }
  const yPad = (yMax - yMin) * 0.15;
  yMin -= yPad;
  yMax += yPad;

  const x = (i: number) =>
    points.length === 1 ? PAD_L + plotW / 2 : PAD_L + (plotW * i) / (points.length - 1);
  const y = (r: number) => PAD_T + plotH * (1 - (r - yMin) / (yMax - yMin));

  const pathD = points
    .map((p, i) => `${i === 0 ? "M" : "L"} ${x(i).toFixed(1)} ${y(p.reward).toFixed(1)}`)
    .join(" ");

  // Y 轴刻度（3 档）
  const yTicks = [yMin, (yMin + yMax) / 2, yMax];

  return (
    <div className="reward-chart-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} className="reward-chart" role="img" aria-label="reward 趋势">
        {/* 水平网格线（极淡）*/}
        {yTicks.map((t, i) => (
          <g key={i}>
            <line
              x1={PAD_L}
              x2={W - PAD_R}
              y1={y(t)}
              y2={y(t)}
              stroke="var(--border-soft)"
              strokeWidth={1}
              strokeDasharray="2 4"
            />
            <text
              x={PAD_L - 8}
              y={y(t) + 3}
              textAnchor="end"
              className="chart-axis mono"
              fill="var(--text-mute)"
            >
              {t.toFixed(2)}
            </text>
          </g>
        ))}

        {/* 折线下方淡填充（accent-soft）*/}
        {points.length > 1 && (
          <path
            d={`${pathD} L ${x(points.length - 1).toFixed(1)} ${(PAD_T + plotH).toFixed(1)} L ${x(0).toFixed(1)} ${(PAD_T + plotH).toFixed(1)} Z`}
            fill="var(--accent-soft)"
            stroke="none"
          />
        )}

        {/* 折线 */}
        {points.length > 1 && (
          <path d={pathD} fill="none" stroke="var(--accent)" strokeWidth={1.8} strokeLinejoin="round" strokeLinecap="round" />
        )}

        {/* 数据点 + hover 区 */}
        {points.map((p, i) => {
          const cx = x(i);
          const cy = y(p.reward);
          const isHover = hover === i;
          const isLast = i === points.length - 1;
          return (
            <g key={i}>
              {/* 透明 hit 区，方便 hover */}
              <circle cx={cx} cy={cy} r={14} fill="transparent" onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} />
              <circle
                cx={cx}
                cy={cy}
                r={isHover ? 4 : isLast ? 3.5 : 2.5}
                fill={isLast ? "var(--accent)" : "var(--bg)"}
                stroke="var(--accent)"
                strokeWidth={1.6}
              />
              {/* X 轴标签：每个点的版本号 */}
              <text
                x={cx}
                y={H - PAD_B + 16}
                textAnchor="middle"
                className="chart-axis mono"
                fill="var(--text-mute)"
              >
                v{p.version}
              </text>
            </g>
          );
        })}

        {/* hover tooltip */}
        {hover !== null && (
          <g className="node-appear">
            <rect
              x={Math.min(Math.max(x(hover) - 48, PAD_L), W - PAD_R - 96)}
              y={y(points[hover].reward) - 38}
              width={96}
              height={26}
              rx={5}
              fill="var(--surface-3)"
              stroke="var(--border)"
            />
            <text
              x={Math.min(Math.max(x(hover), PAD_L + 48), W - PAD_R - 48)}
              y={y(points[hover].reward) - 21}
              textAnchor="middle"
              className="chart-tip mono"
              fill="var(--text)"
            >
              v{points[hover].version} · {points[hover].reward.toFixed(3)}
            </text>
          </g>
        )}
      </svg>
    </div>
  );
}
