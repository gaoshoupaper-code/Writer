import { useEffect, useState, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  getEvolveSession,
  publishEvolve,
  discardEvolve,
  type EvolveSession,
  type DesignChange,
  type EvalFinding,
  type AppliedChange,
} from "@/lib/api";

/**
 * 进化审查报告（全屏路由 /evolve/:sessionId/review）。
 *
 * 审查 pending_review 的进化 session：看"改了什么、为什么改、证据在哪、
 * 实际落地了没"，据此判断是否发布。
 *
 * 数据全部来自 get_session 内联字段（D1）：
 *   design_doc（方案）+ change_log（落地）+ eval_snapshot（评估证据）
 *
 * R7：design_doc 每条 change 对照 change_log.applied 的 design_ref，标注落地状态。
 * R8：design_doc/change_log 缺失 → 不渲染报告区，显示状态 + 提示。
 */
export default function ReviewReport() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const [session, setSession] = useState<EvolveSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [acting, setActing] = useState(false);

  useEffect(() => {
    if (!sessionId) return;
    getEvolveSession(sessionId)
      .then(setSession)
      .catch((err) => toast.error(err instanceof Error ? err.message : "加载失败"))
      .finally(() => setLoading(false));
  }, [sessionId]);

  // 评估 findings 索引（id → finding），供证据引用查找
  const findingsMap = useMemo(() => {
    const m = new Map<string, EvalFinding>();
    for (const f of session?.eval_snapshot?.findings ?? []) {
      if (f.id) m.set(f.id, f);
    }
    return m;
  }, [session]);

  // change_log.applied 按 design_ref 索引（1-based → applied 列表）
  const appliedByRef = useMemo(() => {
    const m = new Map<number, AppliedChange[]>();
    for (const a of session?.change_log?.meta.applied ?? []) {
      if (a.design_ref != null) {
        const list = m.get(a.design_ref) ?? [];
        list.push(a);
        m.set(a.design_ref, list);
      }
    }
    return m;
  }, [session]);

  async function handlePublish() {
    if (!sessionId) return;
    setActing(true);
    try {
      const resp = await publishEvolve(sessionId);
      toast.success(`已发布为 v${resp.snapshot_version}`);
      navigate("/evolve");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "发布失败");
    } finally {
      setActing(false);
    }
  }

  async function handleDiscard() {
    if (!sessionId) return;
    if (!confirm("确认丢弃本次进化结果？将回退到上个生产版本。")) return;
    setActing(true);
    try {
      await discardEvolve(sessionId);
      toast.success("已丢弃");
      navigate("/evolve");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "丢弃失败");
    } finally {
      setActing(false);
    }
  }

  if (loading) return <div className="page-loading">加载审查报告…</div>;
  if (!session) return <div className="page-loading">session 不存在</div>;

  const designDoc = session.design_doc;
  const changeLog = session.change_log;
  const isPendingReview = session.status === "pending_review";
  // R8：报告残缺判断（design_doc 为空 = 方案未产出或不完整）
  const reportIncomplete = !designDoc || !designDoc.meta?.changes;

  return (
    <div className="review-report-page">
      {/* 顶部：session 元信息 */}
      <header className="page-header">
        <button className="config-button ghost small" onClick={() => navigate("/evolve")}>
          ← 返回
        </button>
        <h1>进化审查</h1>
        <span className={`session-status ${session.status}`}>{statusLabel(session.status)}</span>
      </header>

      {/* session 摘要 */}
      <section className="review-summary">
        <div className="summary-grid">
          <div className="summary-item">
            <label>Session</label>
            <span className="mono">{session.session_id.slice(0, 12)}</span>
          </div>
          <div className="summary-item">
            <label>关联评估</label>
            <span className="mono">{session.eval_ref?.slice(0, 12) ?? "—"}</span>
          </div>
          {session.eval_snapshot?.scores && (
            <div className="summary-item">
              <label>评估分数</label>
              <ScoreBadges scores={session.eval_snapshot.scores} />
            </div>
          )}
        </div>
      </section>

      {/* R8：报告残缺 → 拒绝渲染报告区 */}
      {reportIncomplete ? (
        <section className="review-incomplete">
          <div className="incomplete-card">
            <h3>⚠ 报告不完整，无法审查</h3>
            <p>
              本次进化未产出完整的设计文档（design_doc）。
              {session.status === "failed"
                ? "进化过程失败，请查看执行日志定位原因。"
                : "可能是进化尚未完成，或执行过程中断。"}
            </p>
            <p className="text-dim">session 状态：{statusLabel(session.status)}</p>
          </div>
        </section>
      ) : (
        <>
          {/* 改动清单（主轴 R1：改了什么 + 为什么改 + 证据 R2）*/}
          <section className="review-changes">
            <h2 className="section-title">
              改动清单（{designDoc!.meta.changes.length} 条）
            </h2>
            <p className="review-rationale">{designDoc!.body.split("\n").filter((l) => l && !l.startsWith("#")).slice(0, 3).join(" ")}</p>
            <div className="change-list">
              {designDoc!.meta.changes.map((change, i) => (
                <ChangeCard
                  key={i}
                  index={i + 1}
                  change={change}
                  findingsMap={findingsMap}
                  applied={appliedByRef.get(i + 1) ?? []}
                />
              ))}
            </div>
          </section>

          {/* 校验结果（change_log.validation）*/}
          {changeLog?.meta.validation && (
            <section className="review-validation">
              <h2 className="section-title">校验结果</h2>
              <div className={`validation-card ${changeLog.meta.validation.passed ? "passed" : "failed"}`}>
                <span>{changeLog.meta.validation.passed ? "✓ 校验通过" : "✗ 校验失败"}</span>
                {changeLog.meta.validation.errors?.length ? (
                  <ul>
                    {changeLog.meta.validation.errors.map((e, idx) => (
                      <li key={idx} className="mono">{e}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            </section>
          )}
        </>
      )}

      {/* 底部 sticky 操作区（仅 pending_review 可操作）*/}
      {isPendingReview && (
        <div className="review-actions-bar">
          <div className="review-actions-info">
            审查完毕后，发布将固化新 Agent 版本；丢弃将回退到上个生产版本。
          </div>
          <div className="review-actions-buttons">
            <button
              className="config-button secondary"
              onClick={handleDiscard}
              disabled={acting}
            >
              ✗ 丢弃
            </button>
            <button
              className="config-button primary"
              onClick={handlePublish}
              disabled={acting}
            >
              {acting ? "处理中…" : "✓ 发布"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── 改动卡片 ──────────────────────────────────────────────────

function ChangeCard({
  index,
  change,
  findingsMap,
  applied,
}: {
  index: number;
  change: DesignChange;
  findingsMap: Map<string, EvalFinding>;
  applied: AppliedChange[];
}) {
  // R7：落地状态判断（同 design_ref 的所有 applied 都 ok 才算已落地）
  const landedStatus: "landed" | "failed" | "not_landed" =
    applied.length === 0
      ? "not_landed"
      : applied.every((a) => a.result === "ok")
        ? "landed"
        : "failed";

  const statusConfig = {
    landed: { label: "✓ 已落地", color: "var(--teal, #3a9a7e)" },
    failed: { label: "✗ 落地失败", color: "var(--danger, #e5484d)" },
    not_landed: { label: "⚠ 未落地", color: "var(--warn, #f5a623)" },
  }[landedStatus];

  return (
    <div className={`change-card landed-${landedStatus}`}>
      {/* 头部：序号 + 目标 + 落地状态 */}
      <div className="change-card-head">
        <span className="change-index mono">#{index}</span>
        <span className="change-target">{change.target}</span>
        <span className="landed-badge" style={{ color: statusConfig.color }}>
          {statusConfig.label}
        </span>
      </div>

      {/* 改什么 */}
      <div className="change-field">
        <label>改什么</label>
        <p>{change.change_desc}</p>
      </div>

      {/* 为什么 */}
      <div className="change-field">
        <label>为什么</label>
        <p>{change.reason}</p>
      </div>

      {/* 证据（R2：引用评估 finding）*/}
      {change.evidence_ref && change.evidence_ref.length > 0 && (
        <div className="change-field">
          <label>证据（{change.evidence_ref.length} 条）</label>
          <div className="evidence-list">
            {change.evidence_ref.map((refId) => {
              const finding = findingsMap.get(refId);
              return (
                <div key={refId} className={`evidence-item sev-${finding?.severity ?? "unknown"}`}>
                  <div className="evidence-head">
                    <span className="mono evidence-id">{refId}</span>
                    {finding && (
                      <>
                        <span className={`sev-badge ${finding.severity}`}>{finding.severity}</span>
                        <span className="text-dim evidence-dim">{finding.dimension}</span>
                      </>
                    )}
                  </div>
                  {finding && <p className="evidence-finding">{finding.finding}</p>}
                  {finding?.evidence && <p className="text-dim evidence-detail">{finding.evidence}</p>}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* 预期 */}
      <div className="change-expectations">
        {change.expected_up && (
          <div className="expect-up">
            <label>预期↑</label>
            <span>{change.expected_up}</span>
          </div>
        )}
        {change.expected_down && (
          <div className="expect-down">
            <label>预期↓</label>
            <span>{change.expected_down}</span>
          </div>
        )}
      </div>

      {/* 落地详情（R7：显示 applied 记录）*/}
      {applied.length > 0 && (
        <details className="applied-details">
          <summary>落地记录（{applied.length} 条）</summary>
          <div className="applied-list">
            {applied.map((a, i) => (
              <div key={i} className={`applied-item result-${a.result}`}>
                <span className="mono">{a.result === "ok" ? "✓" : "✗"}</span>
                <span className="mono applied-action">[{a.action}]</span>
                <span>{a.target}</span>
                {a.detail && <span className="text-dim applied-detail">{a.detail}</span>}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

// ── 辅助组件 ──────────────────────────────────────────────────

function ScoreBadges({ scores }: { scores: Record<string, any> }) {
  // 从 scores 里提取关键分数（flow_metrics 或 content 总分）
  const items: { label: string; value: string }[] = [];
  const content = scores.content;
  if (content && typeof content === "object") {
    const overall = content.overall ?? content.total;
    if (overall != null) items.push({ label: "内容总分", value: Number(overall).toFixed(1) });
  }
  const flow = scores.flow_metrics;
  if (flow && typeof flow === "object") {
    const errRate = flow.error_rate;
    if (errRate != null) items.push({ label: "错误率", value: `${(Number(errRate) * 100).toFixed(1)}%` });
  }
  if (items.length === 0) return <span className="text-dim">—</span>;
  return (
    <span className="score-badges">
      {items.map((it) => (
        <span key={it.label} className="score-badge">
          {it.label} <strong>{it.value}</strong>
        </span>
      ))}
    </span>
  );
}

function statusLabel(s: string): string {
  const map: Record<string, string> = {
    running: "运行中",
    done: "完成",
    failed: "失败",
    pending_review: "待审查",
    published: "已发布",
    discarded: "已丢弃",
  };
  return map[s] ?? s;
}
