import { useMemo, useState, useRef, useEffect, useCallback } from "react";
import type { TraceDetailLite } from "@/lib/types";

// ── Types ──────────────────────────────────────────────────────────────

type TokenChartPanelProps = {
  detail: TraceDetailLite | null;
  hasActiveThread: boolean;
  activeTraceId: string;
  /** 从图跳转到执行追踪：传入目标 LLM 节点的 node_id */
  onJumpToTrace?: (nodeId: string) => void;
  /** 高亮指定的 loopIndex（从执行追踪跳转过来时使用） */
  highlightedLoopIndex: number | null;
  /** 高亮结束后清除 */
  onClearHighlight?: () => void;
};

type DataPoint = {
  /** Loop 全局序号（X 轴位置） */
  loopIndex: number;
  /** input_tokens（Y 轴值） */
  inputTokens: number;
  agentName: string;
  modelName: string | null;
  /** LLM 调用耗时（ms） */
  durationMs: number | null;
  /** 关联的执行追踪 LLM 节点 node_id（用于双向跳转） */
  nodeId: string;
};

type LoopInfo = {
  displayName: string;
  color: string;
  modelName: string | null;
  inputTokens: number;
  durationMs: number | null;
};

type SeriesDef = {
  key: string;
  name: string;
  color: string;
  points: DataPoint[];
};

// ── Constants ──────────────────────────────────────────────────────────

const SUBAGENT_PALETTE = [
  "#7e5f9e", // violet
  "#e07030", // orange
  "#3a7ec8", // blue
  "#c74a6a", // rose
  "#5a9e3a", // green
  "#c8963a", // amber
  "#5f7ec8", // slate-blue
  "#9e5f7e", // mauve
  "#3ac8a0", // mint
  "#c85a3a", // burnt-orange
];

const MAIN_COLOR = "#3a9a7e";
const MAIN_KEY = "__main__";
const PX_PER_LOOP = 8;
const SVG_H = 530;
const PAD_L = 68;
const PAD_R = 28;
const PAD_T = 20;
const PAD_B = 44;
const PLOT_H = SVG_H - PAD_T - PAD_B;
const DOWNSAMPLE_THRESHOLD = 300;
const DOWNSAMPLE_BUCKET = 4;

// ── Utility ────────────────────────────────────────────────────────────

/** 智能耗时格式化（与执行追踪一致） */
function formatDuration(value: number): string {
  if (value < 1000) return `${value}ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}s`;
  if (value < 3_600_000) return `${(value / 60_000).toFixed(value < 600_000 ? 1 : 0)}min`;
  return `${(value / 3_600_000).toFixed(1)}h`;
}

/** 根据耗时返回颜色类名：<20s 绿 · <60s 橙 · ≥60s 红（与执行追踪一致） */
function durationColorClass(ms: number | null | undefined): string {
  if (ms == null) return "";
  if (ms < 20_000) return "duration-fast";
  if (ms < 60_000) return "duration-medium";
  return "duration-slow";
}

function compactAgentName(name: string) {
  return name.replace(/-subagent$/, "").replace(/-agent$/, "");
}

function agentRole(agentName?: string | null): "main" | "subagent" | null {
  if (!agentName) return null;
  return agentName === "meta-agent" ? "main" : "subagent";
}

// ── R1: 断线处理——按 loopIndex 连续性拆分为多段 ──

function segmentByContinuity(points: DataPoint[]): DataPoint[][] {
  const segments: DataPoint[][] = [];
  let current: DataPoint[] = [];
  for (const p of points) {
    if (current.length === 0 || p.loopIndex === current[current.length - 1].loopIndex + 1) {
      current.push(p);
    } else {
      segments.push(current);
      current = [p];
    }
  }
  if (current.length > 0) segments.push(current);
  return segments;
}

// ── R5d: 降采样——视口外桶采样，视口内全量 ──

function downsampleSeries(
  points: DataPoint[],
  vpStart: number,
  vpEnd: number,
): DataPoint[] {
  if (points.length === 0) return [];

  const inVp: DataPoint[] = [];
  const outVp: DataPoint[] = [];
  for (const p of points) {
    if (p.loopIndex >= vpStart && p.loopIndex <= vpEnd) inVp.push(p);
    else outVp.push(p);
  }

  // 按 DOWNSAMPLE_BUCKET 个 Loop 一组，保留 max + min
  const buckets = new Map<number, DataPoint[]>();
  for (const p of outVp) {
    const bk = Math.floor((p.loopIndex - 1) / DOWNSAMPLE_BUCKET);
    if (!buckets.has(bk)) buckets.set(bk, []);
    buckets.get(bk)!.push(p);
  }
  const sampled: DataPoint[] = [];
  for (const [, bucket] of buckets) {
    if (bucket.length <= 2) {
      sampled.push(...bucket);
      continue;
    }
    let maxPt = bucket[0], minPt = bucket[0];
    for (const p of bucket) {
      if (p.inputTokens > maxPt.inputTokens) maxPt = p;
      if (p.inputTokens < minPt.inputTokens) minPt = p;
    }
    if (maxPt.loopIndex === minPt.loopIndex) {
      sampled.push(maxPt);
    } else {
      // 保持 loopIndex 顺序
      sampled.push(maxPt.loopIndex < minPt.loopIndex ? maxPt : minPt);
      sampled.push(maxPt.loopIndex < minPt.loopIndex ? minPt : maxPt);
    }
  }
  sampled.sort((a, b) => a.loopIndex - b.loopIndex);
  return [...sampled, ...inVp];
}

/**
 * 从 detail.nodes 中筛选 LLM 节点，按 loop 分组提取数据。
 * 并行组的 LLM 节点共享同一个 loopIdx（X 轴对齐）。
 * 每个数据点携带 nodeId 用于双向跳转。
 */
function extractLoops(detail: TraceDetailLite | null) {
  if (!detail) return { main: [], subagents: new Map<string, DataPoint[]>(), maxLoopIndex: 0 };

  const llmNodes = detail.nodes.filter((n) => n.kind === "llm");

  const loops: DataPoint[] = [];
  let loopIdx = 0;
  const groupIndexMap = new Map<string, number>();

  for (const node of llmNodes) {
    // 运行中的节点尚无 usage 数据，跳过以避免 0-token 假点
    if (node.usage?.input_tokens == null) continue;

    if (node.parallel_group_id) {
      const existing = groupIndexMap.get(node.parallel_group_id);
      if (existing !== undefined) {
        // 并行 → 复用已有 loopIdx
        loops.push({
          loopIndex: existing,
          inputTokens: node.usage?.input_tokens ?? 0,
          agentName: node.agent_name ?? "",
          modelName: node.model_name ?? null,
          durationMs: node.duration_ms ?? null,
          nodeId: node.node_id,
        });
        continue;
      }
    }

    loopIdx++;
    if (node.parallel_group_id) {
      groupIndexMap.set(node.parallel_group_id, loopIdx);
    }
    loops.push({
      loopIndex: loopIdx,
      inputTokens: node.usage?.input_tokens ?? 0,
      agentName: node.agent_name ?? "",
      modelName: node.model_name ?? null,
      durationMs: node.duration_ms ?? null,
      nodeId: node.node_id,
    });
  }

  const main: DataPoint[] = [];
  const subagents = new Map<string, DataPoint[]>();

  for (const lp of loops) {
    const role = agentRole(lp.agentName);
    if (role === "main") {
      main.push(lp);
    } else {
      const key = lp.agentName || "unknown";
      if (!subagents.has(key)) subagents.set(key, []);
      subagents.get(key)!.push(lp);
    }
  }

  return { main, subagents, maxLoopIndex: loopIdx };
}

/** Y 轴刻度计算 */
function niceScale(maxValue: number) {
  if (maxValue <= 0) return { niceMax: 1000, step: 250 };
  const rough = maxValue / 4;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const residual = rough / magnitude;
  let niceStep: number;
  if (residual <= 1.5) niceStep = magnitude;
  else if (residual <= 3) niceStep = 2 * magnitude;
  else if (residual <= 7) niceStep = 5 * magnitude;
  else niceStep = 10 * magnitude;

  const niceMax = Math.ceil(maxValue / niceStep) * niceStep;
  return { niceMax: Math.max(niceMax, niceStep), step: niceStep };
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1_000).toFixed(1)}k`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

// ── Component ──────────────────────────────────────────────────────────

export function TokenChartPanel({ detail, hasActiveThread, activeTraceId, onJumpToTrace, highlightedLoopIndex, onClearHighlight }: TokenChartPanelProps) {
  const { main, subagents, maxLoopIndex } = useMemo(
    () => extractLoops(detail),
    [detail],
  );
  const hasData = maxLoopIndex > 0;

  // ── 高亮效果：从执行追踪跳转过来时，滚动到目标位置并高亮 ──
  useEffect(() => {
    if (highlightedLoopIndex == null) return;
    const c = containerRef.current;
    if (!c || !hasData) return;
    // 滚动到高亮 loop 位置
    const targetX = PAD_L + (highlightedLoopIndex - 1) * PX_PER_LOOP - c.clientWidth / 2;
    c.scrollLeft = Math.max(0, targetX);
    // 3 秒后自动清除高亮
    const timer = setTimeout(() => {
      onClearHighlight?.();
    }, 3000);
    return () => clearTimeout(timer);
  }, [highlightedLoopIndex, hasData]);

  // 统一序列列表（main + subagents）
  const allSeries: SeriesDef[] = useMemo(() => {
    const list: SeriesDef[] = [];
    if (main.length > 0) {
      list.push({ key: MAIN_KEY, name: "meta-agent", color: MAIN_COLOR, points: main });
    }
    let idx = 0;
    for (const [name, points] of subagents) {
      list.push({ key: name, name, color: SUBAGENT_PALETTE[idx % SUBAGENT_PALETTE.length], points });
      idx++;
    }
    return list;
  }, [main, subagents]);

  const totalDurationMs = useMemo(
    () => allSeries.reduce((s, ser) => s + ser.points.reduce((a, p) => a + (p.durationMs ?? 0), 0), 0),
    [allSeries],
  );

  // ── 状态 ──

  const [hoverLoopIndex, setHoverLoopIndex] = useState<number | null>(null);
  const [mousePosition, setMousePosition] = useState<{ x: number; y: number } | null>(null);
  const [hiddenSeries, setHiddenSeries] = useState<Set<string>>(new Set());
  const [viewportRange, setViewportRange] = useState<[number, number]>([1, 150]);

  // ── Refs ──

  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  // ── 切换 trace 时重置状态 ──

  useEffect(() => {
    setHiddenSeries(new Set());
    setHoverLoopIndex(null);
    setMousePosition(null);
  }, [activeTraceId]);

  // ── 自动跟随：新 Loop 到达 + 初始定位到最右 ──

  useEffect(() => {
    const c = containerRef.current;
    if (!c || !hasData) return;
    c.scrollLeft = c.scrollWidth;
    // 同步视口范围
    const start = Math.max(1, Math.floor(c.scrollLeft / PX_PER_LOOP) + 1);
    const end = Math.min(maxLoopIndex, start + Math.ceil(c.clientWidth / PX_PER_LOOP));
    setViewportRange([start, end]);
  }, [maxLoopIndex, hasData, activeTraceId]);

  // ── R2: 滚轮劫持 deltaY → scrollLeft ──

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const handleWheel = (e: WheelEvent) => {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        e.preventDefault();
        container.scrollLeft += e.deltaY;
      }
    };
    container.addEventListener("wheel", handleWheel, { passive: false });
    return () => container.removeEventListener("wheel", handleWheel);
  }, []);

  // ── 视口范围跟踪（降采样依赖） ──

  const handleScroll = useCallback(() => {
    const c = containerRef.current;
    if (!c) return;
    const start = Math.max(1, Math.floor(c.scrollLeft / PX_PER_LOOP) + 1);
    const end = Math.min(maxLoopIndex, start + Math.ceil(c.clientWidth / PX_PER_LOOP));
    setViewportRange([start, end]);
  }, [maxLoopIndex]);

  // ── R5a: 鼠标事件 → 十字线 + 信息卡 ──

  const handleMouseMove = useCallback(
    (e: React.MouseEvent<SVGSVGElement>) => {
      const container = containerRef.current;
      if (!container) return;
      const rect = container.getBoundingClientRect();
      // SVG 坐标 = 鼠标在容器内偏移 + 滚动偏移（1:1 viewBox 映射）
      const svgX = (e.clientX - rect.left) + container.scrollLeft;
      const loopIdx = Math.round((svgX - PAD_L) / PX_PER_LOOP) + 1;
      setHoverLoopIndex(Math.max(1, Math.min(loopIdx, maxLoopIndex)));
      setMousePosition({ x: e.clientX - rect.left, y: e.clientY - rect.top });
    },
    [maxLoopIndex],
  );

  const handleMouseLeave = useCallback(() => {
    setHoverLoopIndex(null);
    setMousePosition(null);
  }, []);

  // ── R5c: 图例交互 ──

  const toggleSeries = useCallback((key: string) => {
    setHiddenSeries((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const showAllSeries = useCallback(() => setHiddenSeries(new Set()), []);

  // ── 计算值 ──

  // R5d: 降采样 + R1: 分段
  const shouldDownsample = maxLoopIndex > DOWNSAMPLE_THRESHOLD;
  const processedSeries = useMemo(() => {
    return allSeries.map((s) => {
      const pts = shouldDownsample
        ? downsampleSeries(s.points, viewportRange[0], viewportRange[1])
        : s.points;
      return {
        ...s,
        processedPoints: pts,
        segments: segmentByContinuity(pts),
        // 色带始终基于原始数据（真实活动区间）
        bandSegments: segmentByContinuity(s.points),
      };
    });
  }, [allSeries, shouldDownsample, viewportRange]);

  // Y 轴
  const visiblePoints = processedSeries
    .filter((s) => !hiddenSeries.has(s.key))
    .flatMap((s) => s.processedPoints);
  const maxInput = visiblePoints.length > 0 ? Math.max(...visiblePoints.map((p) => p.inputTokens)) : 1000;
  const { niceMax, step } = niceScale(maxInput);
  const tickCount = Math.round(niceMax / step);

  // R3: SVG 尺寸（高度 530，宽度动态）
  const totalSvgWidth = PAD_L + maxLoopIndex * PX_PER_LOOP + PAD_R;

  // 坐标映射：固定像素间距
  const xScale = (idx: number) => PAD_L + (idx - 1) * PX_PER_LOOP;
  const yScale = (val: number) => PAD_T + PLOT_H - (val / niceMax) * PLOT_H;

  // Y 轴刻度
  const yTicks = Array.from({ length: tickCount + 1 }, (_, i) => {
    const val = i * step;
    return { val, y: yScale(val), label: fmtTokens(val) };
  });

  // X 轴刻度
  const xTickInterval = maxLoopIndex <= 30 ? 1 : maxLoopIndex <= 100 ? 5 : 10;
  const xTicks = Array.from({ length: maxLoopIndex }, (_, i) => i + 1).filter(
    (idx) => idx === 1 || idx === maxLoopIndex || idx % xTickInterval === 0,
  );

  // R5a: Hover 信息卡数据
  const hoverInfo: LoopInfo[] = useMemo(() => {
    if (hoverLoopIndex === null) return [];
    return processedSeries
      .filter((s) => !hiddenSeries.has(s.key))
      .flatMap((s) => {
        const p = s.processedPoints.find((pt) => pt.loopIndex === hoverLoopIndex);
        if (!p) return [];
        return [{
          displayName: s.key === MAIN_KEY ? "Main Agent" : compactAgentName(s.name),
          color: s.color,
          modelName: p.modelName,
          inputTokens: p.inputTokens,
          durationMs: p.durationMs,
        }];
      });
  }, [hoverLoopIndex, processedSeries, hiddenSeries]);

  // ── 空状态（沿用现有） ──

  if (!hasActiveThread) {
    return (
      <div className="token-chart-panel">
        <div className="token-chart-empty">
          <span className="card-line" />
          <h3>先选择一个会话</h3>
          <p>Token 图检测按会话归档，选择会话后才能查看数据。</p>
        </div>
      </div>
    );
  }

  if (!activeTraceId) {
    return (
      <div className="token-chart-panel">
        <div className="token-chart-empty">
          <span className="card-line" />
          <h3>选择一次 Trace</h3>
          <p>在"执行追踪"页选择一条记录，即可查看 Token 图检测。</p>
        </div>
      </div>
    );
  }

  if (!hasData) {
    return (
      <div className="token-chart-panel">
        <div className="token-chart-empty">
          <span className="card-line" />
          <h3>暂无 Token 数据</h3>
          <p>该 Trace 没有 LLM Token 使用数据，或尚未完成加载。</p>
        </div>
      </div>
    );
  }

  // 信息卡边界翻转判断
  const containerWidth = containerRef.current?.clientWidth ?? 1200;
  const infoCardWidth = 240;

  return (
    <div className="token-chart-panel">
      <div className="token-chart-container">
        {/* 图表区域：滚动容器 + HTML overlay 信息卡 */}
        <div className="token-chart-viewport">
          <div
            className="token-chart-scroll-container"
            ref={containerRef}
            onScroll={handleScroll}
          >
            <svg
              ref={svgRef}
              width={totalSvgWidth}
              height={SVG_H}
              viewBox={`0 0 ${totalSvgWidth} ${SVG_H}`}
              onMouseMove={handleMouseMove}
              onMouseLeave={handleMouseLeave}
              style={{ display: "block" }}
            >
              {/* Y 轴网格线 + 标签 */}
              {yTicks.map((t) => (
                <g key={`yt-${t.val}`}>
                  <line
                    className="token-chart-grid-line"
                    x1={PAD_L}
                    y1={t.y}
                    x2={totalSvgWidth - PAD_R}
                    y2={t.y}
                  />
                  <text
                    className="token-chart-axis-label"
                    x={PAD_L - 8}
                    y={t.y}
                    textAnchor="end"
                    dominantBaseline="middle"
                  >
                    {t.label}
                  </text>
                </g>
              ))}

              {/* X 轴标签 */}
              {xTicks.map((idx) => (
                <text
                  className="token-chart-axis-label"
                  key={`xt-${idx}`}
                  x={xScale(idx)}
                  y={SVG_H - PAD_B + 20}
                  textAnchor="middle"
                  dominantBaseline="hanging"
                >
                  {idx}
                </text>
              ))}

              {/* 坐标轴线 */}
              <line x1={PAD_L} y1={PAD_T} x2={PAD_L} y2={PAD_T + PLOT_H} stroke="var(--line)" strokeWidth={1} />
              <line x1={PAD_L} y1={PAD_T + PLOT_H} x2={totalSvgWidth - PAD_R} y2={PAD_T + PLOT_H} stroke="var(--line)" strokeWidth={1} />

              {/* 轴标题 */}
              <text className="token-chart-axis-title" x={PAD_L + (totalSvgWidth - PAD_L - PAD_R) / 2} y={SVG_H - 4} textAnchor="middle">
                Loop 序号
              </text>
              <text
                className="token-chart-axis-title"
                x={14}
                y={PAD_T + PLOT_H / 2}
                textAnchor="middle"
                dominantBaseline="middle"
                transform={`rotate(-90, 14, ${PAD_T + PLOT_H / 2})`}
              >
                输入 Token 数
              </text>

              {/* R5b: Agent 切换背景色带（渲染在折线层之下） */}
              {processedSeries
                .filter((s) => !hiddenSeries.has(s.key))
                .map((s) =>
                  s.bandSegments.map((seg, si) => {
                    const first = seg[0].loopIndex;
                    const last = seg[seg.length - 1].loopIndex;
                    return (
                      <rect
                        key={`band-${s.key}-${si}`}
                        x={xScale(first) - PX_PER_LOOP / 2}
                        y={PAD_T}
                        width={(last - first + 1) * PX_PER_LOOP}
                        height={PLOT_H}
                        fill={s.color}
                        opacity={0.08}
                      />
                    );
                  }),
                )}

              {/* 折线 + 节点（按 series 渲染，SubAgent 在下，Main 在上） */}
              {processedSeries
                .filter((s) => !hiddenSeries.has(s.key))
                .map((s) => (
                  <g key={`series-${s.key}`}>
                    {s.segments.map((seg, si) => (
                      <g key={`seg-${si}`}>
                        {seg.length > 1 && (
                          <polyline
                            fill="none"
                            stroke={s.color}
                            strokeWidth={1.8}
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            points={seg.map((p) => `${xScale(p.loopIndex)},${yScale(p.inputTokens)}`).join(" ")}
                          />
                        )}
                        {seg.map((p) => (
                          <circle
                            key={`d-${s.key}-${p.loopIndex}`}
                            className={`chart-data-point ${highlightedLoopIndex === p.loopIndex ? "chart-point-highlighted" : ""}`}
                            fill={s.color}
                            stroke={highlightedLoopIndex === p.loopIndex ? "var(--accent)" : "#fff"}
                            strokeWidth={highlightedLoopIndex === p.loopIndex ? 3 : 1}
                            cx={xScale(p.loopIndex)}
                            cy={yScale(p.inputTokens)}
                            r={highlightedLoopIndex === p.loopIndex ? 6 : 2.5}
                            style={{ cursor: onJumpToTrace ? "pointer" : "default" }}
                            onClick={(e) => {
                              e.stopPropagation();
                              onJumpToTrace?.(p.nodeId);
                            }}
                          />
                        ))}
                      </g>
                    ))}
                  </g>
                ))}

              {/* R5a: 十字线（垂直虚线 snap 到最近 Loop） */}
              {hoverLoopIndex !== null && (
                <line
                  className="token-chart-crosshair"
                  x1={xScale(hoverLoopIndex)}
                  y1={PAD_T}
                  x2={xScale(hoverLoopIndex)}
                  y2={PAD_T + PLOT_H}
                />
              )}
            </svg>
          </div>

          {/* R5a: HTML Overlay 信息卡 */}
          {hoverLoopIndex !== null && mousePosition && hoverInfo.length > 0 && (
            <div
              className="token-chart-info-card"
              style={{
                left:
                  mousePosition.x + 12 + infoCardWidth > containerWidth
                    ? mousePosition.x - 12 - infoCardWidth
                    : mousePosition.x + 12,
                top: Math.max(0, mousePosition.y - 20),
              }}
            >
              <div className="token-chart-info-title">Loop #{hoverLoopIndex}</div>
              {hoverInfo.map((info) => (
                <div key={info.displayName} className="token-chart-info-row">
                  <span className="token-chart-info-dot" style={{ background: info.color }} />
                  <span className="token-chart-info-name">{info.displayName}</span>
                  <span className="token-chart-info-detail">
                    {fmtTokens(info.inputTokens)} tokens{info.durationMs != null ? " · " : ""}{info.durationMs != null ? <span className={`token-chart-duration ${durationColorClass(info.durationMs)}`}>{formatDuration(info.durationMs)}</span> : null}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* R5c: 图例（可点击显隐系列，双击重置） */}
        <div className="token-chart-legend" onDoubleClick={showAllSeries}>
          {allSeries.map((s) => {
            const hidden = hiddenSeries.has(s.key);
            const display = s.key === MAIN_KEY ? "Main Agent" : compactAgentName(s.name);
            return (
              <span
                key={`legend-${s.key}`}
                className={`token-chart-legend-item ${hidden ? "legend-hidden" : ""}`}
                onClick={() => toggleSeries(s.key)}
              >
                <span
                  className={`token-chart-legend-dot ${hidden ? "dot-hollow" : ""}`}
                  style={hidden ? { borderColor: s.color } : { background: s.color }}
                />
                {display} ({s.points.length} Loops)
              </span>
            );
          })}
          <span className="token-chart-legend-item token-chart-summary">
            共 {maxLoopIndex} Loops · 总耗时 {formatDuration(totalDurationMs)}
          </span>
        </div>
      </div>
    </div>
  );
}
