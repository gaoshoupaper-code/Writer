/**
 * 证据回钻抽屉（FR-005 / DEC-005）。
 *
 * 两种核验对象，统一在右侧抽屉呈现：
 *  - evidence：受控回钻的原始事件（调 drillEvidence API，经 owner 权限 + 索引校验）
 *  - artifact：卷宗内冻结产物正文（直接读 facts.deliveries 的 content_frozen，不走网络）
 *
 * 失败语义（FR-005）：请求失败 / 无权 / ID 不属索引时，不残留上一份证据内容，
 * 直接显示错误并提供重试与关闭。
 *
 * 安全渲染（CON-005）：原始事件与冻结产物都按不可信内容处理，
 * 仅以纯文本 / 结构化 JSON 呈现，禁止 dangerouslySetInnerHTML 或 HTML 解释。
 */

import { useEffect, useState } from "react";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetClose,
} from "@/components/ui/sheet";
import { X, AlertCircle, FileText, Zap, RefreshCw } from "lucide-react";

import { drillEvidence } from "@/lib/api";

// ── 核验对象（抽屉的输入） ────────────────────────────────────

export type DrillTarget =
  | {
      kind: "evidence";
      dossierId: string;
      evidenceId: string;
    }
  | {
      kind: "artifact";
      path: string;
      /** 冻结正文（来自 facts.deliveries.*.content_frozen） */
      content: string | null;
      charCount?: number | null;
      truncated?: boolean;
    };

interface EvidenceSheetProps {
  target: DrillTarget | null;
  onClose: () => void;
}

// 抽屉加载状态
type LoadState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ok"; event: Record<string, unknown> }
  | { status: "error"; message: string };

export function EvidenceSheet({ target, onClose }: EvidenceSheetProps) {
  const open = target !== null;

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="evidence-sheet">
        <SheetHeader>
          <SheetTitle>
            {target?.kind === "evidence"
              ? "证据原始事件"
              : target?.kind === "artifact"
                ? "冻结产物正文"
                : ""}
          </SheetTitle>
          <SheetClose asChild>
            <button className="evidence-sheet-close" aria-label="关闭抽屉">
              <X size={16} />
            </button>
          </SheetClose>
        </SheetHeader>

        {target && (
          <div className="evidence-sheet-body">
            {target.kind === "evidence" ? (
              <EvidenceBody
                key={target.evidenceId}
                dossierId={target.dossierId}
                evidenceId={target.evidenceId}
              />
            ) : (
              <ArtifactBody
                path={target.path}
                content={target.content}
                charCount={target.charCount}
                truncated={target.truncated}
              />
            )}
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}

// ── 证据原始事件（走受控回钻 API） ─────────────────────────────

function EvidenceBody({
  dossierId,
  evidenceId,
}: {
  dossierId: string;
  evidenceId: string;
}) {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  const load = async () => {
    setState({ status: "loading" });
    try {
      const resp = await drillEvidence(dossierId, evidenceId);
      setState({ status: "ok", event: resp.event });
    } catch (e) {
      // FR-005 失败语义：不残留上一份证据，直接显示错误
      const msg = e instanceof Error ? e.message : "证据加载失败";
      setState({ status: "error", message: msg });
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dossierId, evidenceId]);

  return (
    <>
      <div className="evidence-meta">
        <span className="evidence-meta-row">
          <Zap size={12} aria-hidden />
          <span className="evidence-meta-label">证据 ID</span>
          <code className="evidence-meta-value">{evidenceId}</code>
        </span>
        <span className="evidence-meta-row">
          <span className="evidence-meta-label">来源卷宗</span>
          <code className="evidence-meta-value">{dossierId.slice(0, 12)}…</code>
        </span>
      </div>

      {state.status === "loading" && (
        <div className="evidence-loading">加载原始事件…</div>
      )}

      {state.status === "error" && (
        <div className="evidence-error">
          <AlertCircle size={20} aria-hidden />
          <p className="evidence-error-title">证据加载失败</p>
          <p className="evidence-error-msg">{state.message}</p>
          <p className="evidence-error-hint">
            可能原因：无权访问、证据 ID 不在卷宗索引中、或服务端暂时不可用。
          </p>
          <button className="evidence-retry" onClick={load}>
            <RefreshCw size={13} aria-hidden /> 重试
          </button>
        </div>
      )}

      {state.status === "ok" && (
        <pre className="evidence-pre">{safeStringify(state.event)}</pre>
      )}
    </>
  );
}

// ── 冻结产物正文（卷宗内 content_frozen，不走网络） ──────────────

function ArtifactBody({
  path,
  content,
  charCount,
  truncated,
}: {
  path: string;
  content: string | null;
  charCount?: number | null;
  truncated?: boolean;
}) {
  return (
    <>
      <div className="evidence-meta">
        <span className="evidence-meta-row">
          <FileText size={12} aria-hidden />
          <span className="evidence-meta-label">产物路径</span>
          <code className="evidence-meta-value" title={path}>
            {path}
          </code>
        </span>
        {charCount != null && (
          <span className="evidence-meta-row">
            <span className="evidence-meta-label">字符数</span>
            <span className="evidence-meta-value">{charCount.toLocaleString()}</span>
          </span>
        )}
      </div>

      {truncated && (
        <div className="evidence-truncated">
          ⚠ 该产物正文在编纂时已被截断，仅展示冻结片段。
        </div>
      )}

      {content ? (
        <pre className="evidence-pre evidence-pre-text">{content}</pre>
      ) : (
        <div className="evidence-empty">
          该产物无冻结正文（历史卷宗或产物过大未冻结）。
        </div>
      )}
    </>
  );
}

// ── 工具 ──────────────────────────────────────────────────────

/**
 * 安全序列化为可读 JSON。
 *
 * 大对象可能含循环引用或 BigInt，这里兜底处理，避免渲染崩溃。
 * CON-005：仅作为纯文本呈现，不解释 HTML。
 */
function safeStringify(obj: unknown): string {
  try {
    return JSON.stringify(obj, null, 2);
  } catch {
    return String(obj);
  }
}
