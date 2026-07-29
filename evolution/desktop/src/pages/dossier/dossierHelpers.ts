/**
 * 证据卷宗工作台 — 纯函数工具集。
 *
 * 收敛三类关注点，保证组件层只管呈现：
 *  1. 状态 → 中文文案 + 图标 + 语义色映射（NFR-002：不依赖颜色，状态同时具备文案/图标/形状）
 *  2. 四层数据（manifest/facts/semantic/index）的安全取值与空判定（兼容历史卷宗缺字段，不崩页）
 *  3. 候选 trace 的本地搜索/筛选计算（FR-002：名称或 ID 搜索 + 状态筛选 + 最近优先）
 *
 * 这里不引入 React，全部纯函数，便于核对与回归。
 */

import {
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Loader2,
  Archive,
  Ban,
  HelpCircle,
  Circle,
  type LucideIcon,
} from "lucide-react";

import type { Dossier, TraceListItem } from "@/lib/api";

// ── 1. 状态映射 ────────────────────────────────────────────────

export type StatusTone =
  | "success"
  | "warning"
  | "danger"
  | "info"
  | "cancelled"
  | "muted";

export interface DossierStatusMeta {
  /** 业务文案，产品负责人可直接读懂 */
  label: string;
  /** lucide 图标，与文案并呈，不依赖颜色区分状态 */
  icon: LucideIcon;
  /** 语义色 tone，对应 .evo-status-* 与 --evo-* token */
  tone: StatusTone;
  /** 该状态是否可进入评估（CON-004：ready 才能评估的不变量） */
  evaluable: boolean;
  /** 编译中态（用于显示 spinner 与停止入口） */
  compiling: boolean;
}

/**
 * 卷宗状态 → 展示元信息。
 *
 * 后端落库的 status 比前端 Dossier 类型注释更全（会落 cancelled），
 * 这里以「全集 + 兜底」方式映射，避免未知态直接裸出英文。
 */
const DOSSIER_STATUS_TABLE: Record<string, DossierStatusMeta> = {
  ready: {
    label: "完整·可评估",
    icon: CheckCircle2,
    tone: "success",
    evaluable: true,
    compiling: false,
  },
  partial: {
    label: "降级·不可评估",
    icon: AlertTriangle,
    tone: "warning",
    evaluable: false,
    compiling: false,
  },
  failed: {
    label: "编译失败",
    icon: XCircle,
    tone: "danger",
    evaluable: false,
    compiling: false,
  },
  compiling: {
    label: "编译中",
    icon: Loader2,
    tone: "info",
    evaluable: false,
    compiling: true,
  },
  pending: {
    label: "等待编译",
    icon: Loader2,
    tone: "info",
    evaluable: false,
    compiling: true,
  },
  superseded: {
    label: "已废弃",
    icon: Archive,
    tone: "cancelled",
    evaluable: false,
    compiling: false,
  },
  cancelled: {
    label: "已取消",
    icon: Ban,
    tone: "cancelled",
    evaluable: false,
    compiling: false,
  },
};

const DOSSIER_STATUS_FALLBACK: DossierStatusMeta = {
  label: "未知状态",
  icon: HelpCircle,
  tone: "muted",
  evaluable: false,
  compiling: false,
};

export function getDossierStatusMeta(status: string): DossierStatusMeta {
  return DOSSIER_STATUS_TABLE[status] ?? DOSSIER_STATUS_FALLBACK;
}

// ── trace 状态映射（候选列表用，复用同一套语义色） ───────────────

export interface TraceStatusMeta {
  label: string;
  icon: LucideIcon;
  tone: StatusTone;
}

const TRACE_STATUS_TABLE: Record<string, TraceStatusMeta> = {
  pending: { label: "待启动", icon: Circle, tone: "muted" },
  running: { label: "运行中", icon: Loader2, tone: "info" },
  awaiting_input: { label: "等待输入", icon: AlertTriangle, tone: "warning" },
  cancelling: { label: "取消中", icon: Loader2, tone: "warning" },
  completed: { label: "已完成", icon: CheckCircle2, tone: "success" },
  failed: { label: "已失败", icon: XCircle, tone: "danger" },
  cancelled: { label: "已取消", icon: Ban, tone: "cancelled" },
  cancel_timeout: { label: "取消超时", icon: AlertTriangle, tone: "warning" },
  interrupted: { label: "已中断", icon: AlertTriangle, tone: "warning" },
};

const TRACE_STATUS_FALLBACK: TraceStatusMeta = {
  label: "未知",
  icon: HelpCircle,
  tone: "muted",
};

export function getTraceStatusMeta(status: string): TraceStatusMeta {
  return TRACE_STATUS_TABLE[status] ?? TRACE_STATUS_FALLBACK;
}

// ── 覆盖矩阵项状态映射（清单层 contract_coverage_matrix.items[].status） ──

export interface CoverageItemMeta {
  label: string;
  icon: LucideIcon;
  tone: StatusTone;
}

const COVERAGE_ITEM_TABLE: Record<string, CoverageItemMeta> = {
  covered: { label: "已覆盖", icon: CheckCircle2, tone: "success" },
  missing: { label: "缺口", icon: XCircle, tone: "danger" },
  na: { label: "不适用", icon: Circle, tone: "muted" },
};

const COVERAGE_ITEM_FALLBACK: CoverageItemMeta = {
  label: "未知",
  icon: HelpCircle,
  tone: "muted",
};

export function getCoverageItemMeta(status: string): CoverageItemMeta {
  return COVERAGE_ITEM_TABLE[status] ?? COVERAGE_ITEM_FALLBACK;
}

// ── 2. 四层数据安全取值 ────────────────────────────────────────
//
// 后端四层是 Record<string, any>，历史卷宗可能缺层或缺字段。
// 这里集中做 null-safe 取值，组件层不重复写 || [] / || {} 防御。

export interface CoverageMatrix {
  items: Array<{
    dim: string;
    key: string;
    status: string;
    reason?: string | null;
    evidence?: string | null;
  }>;
  covered_count: number;
  missing_count: number;
  na_count: number;
  complete: boolean;
}

/** 从清单层安全取出契约覆盖矩阵（可能为空） */
export function getCoverageMatrix(manifest: Dossier["manifest"]): CoverageMatrix | null {
  const m = manifest as Record<string, any> | null;
  const matrix = m?.contract_coverage_matrix;
  if (!matrix || typeof matrix !== "object") return null;
  const items = Array.isArray(matrix.items) ? matrix.items : [];
  return {
    items: items.map((it: any) => ({
      dim: String(it?.dim ?? ""),
      key: String(it?.key ?? ""),
      status: String(it?.status ?? ""),
      reason: it?.reason ?? null,
      evidence: it?.evidence ?? null,
    })),
    covered_count: Number(matrix.covered_count ?? 0),
    missing_count: Number(matrix.missing_count ?? 0),
    na_count: Number(matrix.na_count ?? 0),
    complete: Boolean(matrix.complete),
  };
}

/** 清单层完整度（full/partial），可能为空字符串 */
export function getCompleteness(manifest: Dossier["manifest"]): string {
  const m = manifest as Record<string, any> | null;
  const v = m?.completeness;
  return v ? String(v) : "";
}

/** 清单层适用维度（通用五维 / 玄幻正文十一维） */
export function getApplicableDimensions(manifest: Dossier["manifest"]): string[] {
  const m = manifest as Record<string, any> | null;
  const dims = m?.applicable_dimensions;
  return Array.isArray(dims) ? dims.map(String) : [];
}

/** 清单层运行摘要状态（run_status） */
export function getRunStatus(manifest: Dossier["manifest"]): string {
  const m = manifest as Record<string, any> | null;
  const v = m?.run_status;
  return v ? String(v) : "";
}

/**
 * 事实层结构化视图。
 *
 * 直接对应 extractor.py 产出的类别，组件层只读这里的字段，不再各自解构 facts。
 */
export interface FactsView {
  runSummary: {
    status: string;
    endpoint: string;
    durationMs: number | null;
    eventCount: number;
    error: string | null;
    startedAt: string | null;
    endedAt: string | null;
  } | null;
  contract: {
    available: boolean;
    userGoal: string | null;
    taskType: string | null;
    promisedArtifacts: string[];
    missing: string[];
  } | null;
  deliveries: Array<{
    agent: string;
    files: Array<{
      path: string;
      charCount: number | null;
      truncated: boolean;
      contentFrozen: string | null;
    }>;
  }>;
  reviewChain: Array<{
    reviewer: string;
    reviewFile: string | null;
    status: string | null;
    evidenceId: string | null;
    rawEventId: string | null;
    isRecheck: boolean;
    findings: string | null;
    sequence: number | null;
  }>;
  reviseEvents: Array<{
    subagent: string | null;
    revisedPath: string | null;
    confidence: string | null;
    evidenceId: string | null;
    note: string | null;
  }>;
  recoveryChain: Array<{
    errorType: string | null;
    agentName: string | null;
    errorMessage: string | null;
    recoveryStatus: string | null;
    evidenceId: string | null;
    note: string | null;
  }>;
  memoryQuality: {
    available: boolean;
    totalRetrievals: number;
    okCount: number;
    failCount: number;
    totalNodes: number | null;
    totalEdges: number | null;
  } | null;
  coverage: {
    stageKinds: string[];
    agentCount: number;
    agentNames: string[];
    deliveryFileCount: number;
    reviewCalls: number;
    reviseInferred: number;
    errorEvents: number;
  } | null;
}

export function getFactsView(facts: Dossier["facts"]): FactsView {
  const f = facts as Record<string, any> | null;
  const rs = f?.run_summary as Record<string, any> | undefined;
  const ct = f?.contract as Record<string, any> | undefined;
  const mq = f?.memory_quality as Record<string, any> | undefined;
  const cov = f?.coverage as Record<string, any> | undefined;
  const deliveriesRaw = f?.deliveries as Record<string, any> | undefined;

  const deliveries: FactsView["deliveries"] = deliveriesRaw
    ? Object.entries(deliveriesRaw).map(([agent, files]) => {
        const filesObj = (files ?? {}) as Record<string, any>;
        return {
          agent,
          files: Object.entries(filesObj).map(([path, meta]) => {
            const m = (meta ?? {}) as Record<string, any>;
            return {
              path,
              charCount: m.char_count != null ? Number(m.char_count) : null,
              truncated: Boolean(m.truncated),
              contentFrozen: m.content_frozen ?? null,
            };
          }),
        };
      })
    : [];

  const reviewChainRaw = Array.isArray(f?.review_chain) ? f.review_chain : [];
  const reviseEventsRaw = Array.isArray(f?.revise_events) ? f.revise_events : [];
  const recoveryChainRaw = Array.isArray(f?.recovery_chain) ? f.recovery_chain : [];

  return {
    runSummary: rs
      ? {
          status: String(rs.status ?? ""),
          endpoint: String(rs.endpoint ?? ""),
          durationMs: rs.duration_ms != null ? Number(rs.duration_ms) : null,
          eventCount: Number(rs.event_count ?? 0),
          error: rs.error ?? null,
          startedAt: rs.started_at ?? null,
          endedAt: rs.ended_at ?? null,
        }
      : null,
    contract: ct
      ? {
          available: Boolean(ct.available),
          userGoal: ct.user_goal ?? null,
          taskType: ct.task_type ?? null,
          promisedArtifacts: Array.isArray(ct.promised_artifacts)
            ? ct.promised_artifacts.map(String)
            : [],
          missing: Array.isArray(ct.missing) ? ct.missing.map(String) : [],
        }
      : null,
    deliveries,
    reviewChain: reviewChainRaw.map((r: any) => ({
      reviewer: r?.reviewer ?? "",
      reviewFile: r?.review_file ?? null,
      status: r?.status ?? null,
      evidenceId: r?.evidence_id ?? null,
      rawEventId: r?.raw_event_id ?? null,
      isRecheck: Boolean(r?.is_recheck),
      findings: r?.findings ?? null,
      sequence: r?.sequence != null ? Number(r.sequence) : null,
    })),
    reviseEvents: reviseEventsRaw.map((r: any) => ({
      subagent: r?.subagent ?? null,
      revisedPath: r?.revised_path ?? null,
      confidence: r?.confidence ?? null,
      evidenceId: r?.evidence_id ?? null,
      note: r?.note ?? null,
    })),
    recoveryChain: recoveryChainRaw.map((r: any) => ({
      errorType: r?.error_type ?? null,
      agentName: r?.agent_name ?? null,
      errorMessage: r?.error_message ?? null,
      recoveryStatus: r?.recovery_status ?? null,
      evidenceId: r?.evidence_id ?? null,
      note: r?.note ?? null,
    })),
    memoryQuality: mq
      ? {
          available: Boolean(mq.available),
          totalRetrievals: Number(mq.summary?.total_retrievals ?? 0),
          okCount: Number(mq.summary?.ok_count ?? 0),
          failCount: Number(mq.summary?.fail_count ?? 0),
          totalNodes: mq.summary?.total_nodes != null ? Number(mq.summary.total_nodes) : null,
          totalEdges: mq.summary?.total_edges != null ? Number(mq.summary.total_edges) : null,
        }
      : null,
    coverage: cov
      ? {
          stageKinds: Array.isArray(cov.stage_kinds) ? cov.stage_kinds.map(String) : [],
          agentCount: Number(cov.agent_count ?? 0),
          agentNames: Array.isArray(cov.agent_names) ? cov.agent_names.map(String) : [],
          deliveryFileCount: Number(cov.delivery_file_count ?? 0),
          reviewCalls: Number(cov.review_calls ?? 0),
          reviseInferred: Number(cov.revise_inferred ?? 0),
          errorEvents: Number(cov.error_events ?? 0),
        }
      : null,
  };
}

/**
 * 语义层结构化视图。
 *
 * 语义层是 Agent 归纳，非确定性事实——调用方必须显式标注。
 * 三种降级路径（skipped / error / LLM 上限）通过 status 字段统一暴露。
 */
export interface SemanticView {
  status: "ok" | "skipped" | "error" | "limited" | "empty";
  reason: string | null;
  stages: Array<{
    name: string;
    summary: string | null;
    keyFacts: Array<{
      fact: string;
      evidenceId: string | null;
    }>;
  }>;
  priorities: Array<{
    level: string;
    title: string | null;
    detail: string | null;
    evidenceId: string | null;
  }>;
}

export function getSemanticView(semantic: Dossier["semantic"]): SemanticView {
  const s = semantic as Record<string, any> | null;
  if (!s) {
    return { status: "empty", reason: null, stages: [], priorities: [] };
  }

  let status: SemanticView["status"] = "ok";
  let reason: string | null = null;
  if (s.skipped) {
    status = "skipped";
    reason = s.reason ?? "语义归纳被跳过";
  } else if (s.error) {
    status = "error";
    reason = String(s.error);
  } else if (typeof s.reason === "string" && s.reason.includes("达上限")) {
    status = "limited";
    reason = s.reason;
  }

  const stagesRaw = Array.isArray(s.stages) ? s.stages : [];
  const prioritiesRaw = Array.isArray(s.priorities) ? s.priorities : [];

  return {
    status,
    reason,
    stages: stagesRaw.map((st: any) => ({
      name: String(st?.name ?? st?.stage ?? ""),
      summary: st?.summary ?? st?.narrative ?? null,
      keyFacts: Array.isArray(st?.key_facts)
        ? st.key_facts.map((kf: any) => ({
            fact: typeof kf === "string" ? kf : String(kf?.fact ?? kf?.text ?? ""),
            evidenceId: kf?.evidence_id ?? null,
          }))
        : [],
    })),
    priorities: prioritiesRaw.map((p: any) => ({
      level: String(p?.level ?? ""),
      title: p?.title ?? p?.point ?? null,
      detail: p?.detail ?? p?.detail_text ?? null,
      evidenceId: p?.evidence_id ?? null,
    })),
  };
}

/**
 * 索引层结构化视图。
 *
 * 索引层是受控回钻的依据：evidence_ids 与 artifact_paths。
 */
export interface IndexView {
  evidenceIds: string[];
  artifactPaths: string[];
}

export function getIndexView(index: Dossier["index"]): IndexView {
  const idx = index as Record<string, any> | null;
  return {
    evidenceIds: Array.isArray(idx?.evidence_ids) ? idx.evidence_ids.map(String) : [],
    artifactPaths: Array.isArray(idx?.artifact_paths) ? idx.artifact_paths.map(String) : [],
  };
}

// ── 3. 候选 trace 本地搜索/筛选 ─────────────────────────────────

/**
 * 按名称或 ID 搜索 + 按状态筛选，结果按 started_at 降序（最近优先）。
 *
 * 返回新数组，不修改原数组；筛选用「全部」时不裁剪状态。
 * 搜索词两端 trim 后大小写不敏感匹配 session_name 或 trace_id。
 */
export function filterTraces(
  traces: TraceListItem[],
  search: string,
  statusFilter: string,
): TraceListItem[] {
  const q = search.trim().toLowerCase();
  const out = traces.filter((t) => {
    if (q) {
      const name = (t.session_name ?? "").toLowerCase();
      const id = t.trace_id.toLowerCase();
      if (!name.includes(q) && !id.includes(q)) return false;
    }
    if (statusFilter && statusFilter !== "all" && t.status !== statusFilter) return false;
    return true;
  });
  // 最近优先：started_at 降序；空时间排到末尾
  return out.sort((a, b) => {
    const ta = a.started_at ?? "";
    const tb = b.started_at ?? "";
    if (ta === tb) return 0;
    return ta < tb ? 1 : -1;
  });
}

// ── 4. 显示用文案 ──────────────────────────────────────────────

/**
 * trace 可读标题：session_name 优先，空则降级为截断 trace_id。
 * FR-002 兼容性：session_name 为空不得显示空标题。
 */
export function getTraceTitle(t: TraceListItem): string {
  const name = t.session_name?.trim();
  if (name) return name;
  return t.trace_id.slice(0, 12) + "…";
}

/** 相对时间简述（用于列表项），输入 ISO 字符串 */
export function formatRelativeTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffMs = Date.now() - then;
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return "刚刚";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
}

/** 工具函数：对象是否「可视为空」（null/undefined/空串/空数组/空对象） */
export function isEmptyValue(v: unknown): boolean {
  if (v == null) return true;
  if (typeof v === "string") return v.trim() === "";
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === "object") return Object.keys(v as object).length === 0;
  return false;
}
