// adapt-types.ts —— 进化循环 + 配置版本的前端类型契约（需求 §4.3）。
//
// 对应后端：
//   evolution/app/adapt/api.py        （sessions/start/stop/stream）
//   evolution/app/view/versions_api.py（versions 谱系）
//
// 设计依据：需求基准 D4（SSE）/D8（谱系树）/D11（edits 粒度）。

// ── adapt session ──────────────────────────────────────────

export type SessionStatus =
  | "running"
  | "completed"
  | "terminated"
  | "error";

export type RoundOutcome = "shipped" | "rejected" | "idle" | "";

/** session 列表项（GET /api/adapt/sessions） */
export type AdaptSessionListItem = {
  session_id: string;
  status: SessionStatus;
  round_count: number;
  shipped_count: number;
  baseline_version: number;
  shipped_version: number | null;
  started_at: string;
  last_at: string;
};

/** 一个候选的 edit 指令（evolver 产出，D11 展示粒度） */
export type AdaptEdit = {
  op: "replace" | "insert" | "remove";
  target: string[];
  spec?: Record<string, unknown>;
  manifest?: {
    intent?: string;
    expected_up?: string[];
    expected_down?: string[];
    rationale?: string;
  };
};

/** 一个候选的摘要（不含完整 config，D11） */
export type AdaptCandidateSummary = {
  edits: AdaptEdit[];
  source_commit?: string;
};

/** 一个候选的评估结果 */
export type CandidateResult = {
  candidate_idx: number;
  trace_ids: string[];
  reward: number;
};

/** critic 判决 */
export type CriticVerdict = {
  verdict?: "pass" | "reject" | "revision" | "";
  ranking?: number[];
  feedback?: string;
  target_idx?: number;
  ship_idx?: number;
};

/** 单轮记录（session 详情内） */
export type AdaptRound = {
  round: number;
  landscape: string;
  candidates: AdaptCandidateSummary[];
  round_outcome: RoundOutcome;
  shipped_version: number | null;
  baseline_version: number;
  baseline_scores: Record<string, Record<string, unknown>>;
  candidate_scores: CandidateResult[];
  critic_verdict: CriticVerdict;
  created_at: string;
};

/** session 详情（GET /api/adapt/sessions/{id}） */
export type AdaptSessionDetail = {
  session_id: string;
  status: SessionStatus;
  rounds: AdaptRound[];
  baseline_version: number;
};

// ── SSE 事件（GET /api/adapt/sessions/{id}/stream，D4）─────

export type AdaptStreamEvent =
  | { type: "session_hello"; session_id: string; terminal: string | null }
  | {
      type: "node_output";
      node: AdaptNodeName;
      round: number;
      payload: NodeOutputPayload;
    }
  | { type: "round_end"; round: number; outcome: RoundOutcome; shipped_version: number | null }
  | { type: "session_end"; outcome: SessionStatus; reason?: string }
  | { type: "error"; reason: string };

/** adapt graph 的 9 个节点名（固定流程） */
export type AdaptNodeName =
  | "run_baseline"
  | "planner"
  | "evolver"
  | "run_candidates"
  | "evaluate"
  | "critic"
  | "gate"
  | "ship"
  | "loop_control";

/** 节点产出 payload（按节点类型不同，D4） */
export type NodeOutputPayload = {
  baseline_traces?: string[];
  baseline_scores?: Record<string, Record<string, unknown>>;
  landscape?: string;
  candidates?: AdaptCandidateSummary[];
  candidate_results?: CandidateResult[];
  baseline_reward?: number;
  critic_verdict?: CriticVerdict;
  round_outcome?: RoundOutcome;
  shipped?: boolean;
  round?: number;
  finished?: boolean;
  best_reward?: number;
  idle_count?: number;
};

// ── 配置版本谱系（D8）──────────────────────────────────────

export type VersionStatus = "production" | "retired";

/** 版本列表项（GET /api/versions） */
export type VersionListItem = {
  version: number;
  parent_version: number | null;
  status: VersionStatus;
  change_summary: string | null;
  created_at: string;
  source_commit: string | null;
  reward: number | null;
  source_session: string | null;
  source_round: number | null;
};

/** 版本列表响应 */
export type VersionListResponse = {
  items: VersionListItem[];
  total: number;
  production_version: number | null;
  limit: number;
  offset: number;
};

/** 版本详情（GET /api/versions/{version}，D11：edits + manifest + reward） */
export type VersionDetail = {
  version: number;
  parent_version: number | null;
  status: VersionStatus;
  change_summary: string | null;
  created_at: string;
  source_commit: string | null;
  is_bootstrap: boolean;
  edits: AdaptEdit[];
  reward: number | null;
  baseline_reward: number | null;
  baseline_version: number | null;
  critic_verdict: CriticVerdict;
  source_session: string | null;
  source_round: number | null;
};

// ── 启动请求（D6 可调参数）─────────────────────────────────

export type AdaptStartParams = {
  rounds: number;
  patience: number;
  judge_j: number;
};

export type AdaptStartResponse = {
  session_id: string;
  baseline_version: number;
  batch_size: number;
  status: string;
};
