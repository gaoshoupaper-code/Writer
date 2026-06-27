"use client";

/**
 * NodeOutputView —— 渲染单个节点的产出（驾驶舱中部，D4）。
 *
 * 不同节点产出形态不同，这里做定向渲染：
 *   planner  → landscape（长文本，失败模式分析）
 *   evolver  → 候选 edits（结构化卡片：op/target/manifest）
 *   evaluate → 候选 vs 基准 reward 对比（柱状条）
 *   critic   → verdict + feedback（评语 + reward hacking 检测）
 *   gate     → 轮结果（shipped/rejected）
 *   其他     → 结构化 JSON 摘要
 *
 * 设计：内容驱动排版，不用千篇一律的卡片。landscape 用 prose 阅读，
 * edits 用紧凑表格，reward 用对比条。
 */
import type {
  AdaptCandidateSummary,
  AdaptEdit,
  AdaptNodeName,
  CandidateResult,
  CriticVerdict,
  NodeOutputPayload,
} from "@/lib/adapt-types";

export function NodeOutputView({
  node,
  payload,
}: {
  node: AdaptNodeName;
  payload: NodeOutputPayload;
}) {
  switch (node) {
    case "planner":
      return <LandscapeView text={payload.landscape ?? ""} />;
    case "evolver":
      return <CandidatesView candidates={payload.candidates ?? []} />;
    case "evaluate":
    case "run_candidates":
      return (
        <RewardCompareView
          results={payload.candidate_results ?? []}
          baselineReward={payload.baseline_reward}
        />
      );
    case "critic":
      return <CriticView verdict={payload.critic_verdict ?? {}} />;
    case "gate":
      return <GateView outcome={payload.round_outcome} />;
    case "ship":
      return <ShipView />;
    case "loop_control":
      return <LoopControlView payload={payload} />;
    default:
      return <BaselineView payload={payload} />;
  }
}

// ── planner: landscape（失败模式分析长文）──────────────────

function LandscapeView({ text }: { text: string }) {
  return (
    <div className="node-pane">
      <PaneHeader node="planner" title="Adaptation Landscape" hint="planner 产出的失败模式全景" />
      <div className="landscape-body prose-doc">
        <pre className="landscape-text">{text || "（等待产出…）"}</pre>
      </div>
    </div>
  );
}

// ── evolver: 候选 edits ────────────────────────────────────

function CandidatesView({ candidates }: { candidates: AdaptCandidateSummary[] }) {
  return (
    <div className="node-pane">
      <PaneHeader
        node="evolver"
        title={`候选改进（${candidates.length}）`}
        hint="每个候选是一组 typed edit + manifest"
      />
      {candidates.length === 0 ? (
        <Empty text="（等待产出…）" />
      ) : (
        <div className="candidates-list">
          {candidates.map((c, i) => (
            <CandidateCard key={i} idx={i} candidate={c} />
          ))}
        </div>
      )}
    </div>
  );
}

function CandidateCard({
  idx,
  candidate,
}: {
  idx: number;
  candidate: AdaptCandidateSummary;
}) {
  return (
    <div className="candidate-card">
      <div className="candidate-head">
        <span className="candidate-idx mono">候选 {idx}</span>
        <span className="candidate-meta mono text-mute">
          {candidate.edits.length} edits
          {candidate.source_commit ? ` · ${candidate.source_commit.slice(0, 7)}` : ""}
        </span>
      </div>
      <div className="edits-list">
        {candidate.edits.map((e, j) => (
          <EditRow key={j} edit={e} />
        ))}
      </div>
    </div>
  );
}

function EditRow({ edit }: { edit: AdaptEdit }) {
  const opColor: Record<string, string> = {
    replace: "var(--accent)",
    insert: "var(--completed)",
    remove: "var(--failed)",
  };
  return (
    <div className="edit-row">
      <div className="edit-op-line">
        <span
          className="edit-op mono"
          style={{ color: opColor[edit.op] ?? "var(--text-dim)" }}
        >
          {edit.op}
        </span>
        <span className="edit-target mono">{edit.target.join(" / ")}</span>
      </div>
      {edit.manifest && (
        <div className="edit-manifest">
          {edit.manifest.intent && (
            <div className="manifest-line">
              <span className="manifest-key">意图</span>
              <span>{edit.manifest.intent}</span>
            </div>
          )}
          {(edit.manifest.expected_up?.length || edit.manifest.expected_down?.length) && (
            <div className="manifest-line">
              <span className="manifest-key">预期</span>
              <span className="mono" style={{ fontSize: 11 }}>
                ↑ {edit.manifest.expected_up?.join(", ") || "—"}　↓ {edit.manifest.expected_down?.join(", ") || "—"}
              </span>
            </div>
          )}
          {edit.manifest.rationale && (
            <div className="manifest-line">
              <span className="manifest-key">理由</span>
              <span className="text-dim">{edit.manifest.rationale}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── evaluate: reward 对比 ──────────────────────────────────

function RewardCompareView({
  results,
  baselineReward,
}: {
  results: CandidateResult[];
  baselineReward?: number;
}) {
  const base = baselineReward ?? 0;
  const allVals = [...results.map((r) => r.reward), base];
  const max = Math.max(...allVals, 0.01);
  return (
    <div className="node-pane">
      <PaneHeader node="evaluate" title="Reward 对比" hint="候选 vs 基准" />
      <div className="reward-compare">
        <RewardBar label="基准" value={base} max={max} isBaseline />
        {results.map((r) => (
          <RewardBar
            key={r.candidate_idx}
            label={`候选 ${r.candidate_idx}`}
            value={r.reward}
            max={max}
            traceCount={r.trace_ids.length}
            beat={r.reward > base}
          />
        ))}
      </div>
    </div>
  );
}

function RewardBar({
  label,
  value,
  max,
  isBaseline,
  beat,
  traceCount,
}: {
  label: string;
  value: number;
  max: number;
  isBaseline?: boolean;
  beat?: boolean;
  traceCount?: number;
}) {
  const pct = Math.max(2, (value / max) * 100);
  const color = isBaseline ? "var(--text-dim)" : beat ? "var(--completed)" : "var(--failed)";
  return (
    <div className="reward-bar-row">
      <span className="reward-bar-label mono">{label}</span>
      <div className="reward-bar-track">
        <div
          className="reward-bar-fill"
          style={{ width: `${pct}%`, background: color, opacity: isBaseline ? 0.5 : 0.85 }}
        />
      </div>
      <span className="reward-bar-value mono" style={{ color }}>
        {value.toFixed(3)}
      </span>
      {traceCount != null && (
        <span className="reward-bar-traces mono text-mute">{traceCount} traces</span>
      )}
    </div>
  );
}

// ── critic: verdict + feedback ─────────────────────────────

function CriticView({ verdict }: { verdict: CriticVerdict }) {
  const v = verdict.verdict ?? "";
  const colorMap: Record<string, string> = {
    pass: "var(--completed)",
    reject: "var(--failed)",
    revision: "var(--awaiting)",
    "": "var(--text-mute)",
  };
  return (
    <div className="node-pane">
      <PaneHeader node="critic" title="Critic 审视" hint="reward hacking 防御 + 排序" />
      <div className="critic-verdict">
        <span className="critic-verdict-label mono">verdict</span>
        <span
          className="critic-verdict-value mono"
          style={{ color: colorMap[v] }}
        >
          {v || "（等待…）"}
        </span>
        {verdict.ranking && verdict.ranking.length > 0 && (
          <span className="critic-ranking mono text-mute">
            ranking: [{verdict.ranking.join(", ")}]
          </span>
        )}
      </div>
      {verdict.feedback && (
        <div className="critic-feedback prose-doc">
          <pre className="landscape-text">{verdict.feedback}</pre>
        </div>
      )}
    </div>
  );
}

// ── gate ───────────────────────────────────────────────────

function GateView({ outcome }: { outcome?: string }) {
  const shipped = outcome === "shipped";
  return (
    <div className="node-pane">
      <PaneHeader node="gate" title="门控结果" hint="确定性验收 + seesaw 约束" />
      <div
        className="gate-result"
        style={{
          color: shipped ? "var(--completed)" : outcome ? "var(--failed)" : "var(--text-mute)",
        }}
      >
        {shipped ? "通过 → 待发布" : outcome === "rejected" ? "拒绝（未通过 seesaw / manifest）" : "（等待…）"}
      </div>
    </div>
  );
}

function ShipView() {
  return (
    <div className="node-pane">
      <PaneHeader node="ship" title="发布" hint="存 config 快照 + git push + reload executor" />
      <div className="gate-result" style={{ color: "var(--completed)" }}>
        已发布为新 production 版本，executor 已热加载
      </div>
    </div>
  );
}

function LoopControlView({ payload }: { payload: NodeOutputPayload }) {
  return (
    <div className="node-pane">
      <PaneHeader node="loop_control" title="循环控制" hint="patience / budget 判定" />
      <div className="loop-control-grid">
        <KV label="best_reward" value={payload.best_reward?.toFixed(3) ?? "—"} />
        <KV label="idle_count" value={String(payload.idle_count ?? "—")} />
        <KV label="finished" value={payload.finished ? "是" : "否"} />
      </div>
    </div>
  );
}

function BaselineView({ payload }: { payload: NodeOutputPayload }) {
  const traces = payload.baseline_traces ?? [];
  return (
    <div className="node-pane">
      <PaneHeader
        node="run_baseline"
        title="基准执行"
        hint={`跑 ${traces.length} 个基准 trace`}
      />
      {traces.length === 0 ? (
        <Empty text="（等待产出…）" />
      ) : (
        <div className="baseline-traces">
          {traces.map((t) => (
            <a key={t} href={`/traces/?id=${t}`} className="baseline-trace mono">
              {t.slice(0, 16)}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

// ── 共享小件 ───────────────────────────────────────────────

function PaneHeader({
  node,
  title,
  hint,
}: {
  node: AdaptNodeName;
  title: string;
  hint: string;
}) {
  return (
    <div className="pane-header">
      <div>
        <div className="pane-title">{title}</div>
        <div className="pane-hint text-mute">{hint}</div>
      </div>
      <span className="pane-node mono text-mute">{node}</span>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="text-mute" style={{ padding: "24px 0", textAlign: "center" }}>{text}</div>;
}

function KV({ label, value }: { label: string; value: string }) {
  return (
    <div className="kv-block">
      <span className="kv-label mono">{label}</span>
      <span className="kv-value mono">{value}</span>
    </div>
  );
}
