import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetClose,
} from "@/components/ui/sheet";
import {
  getPromoteTasks,
  decidePromoteTask,
  getDatasetCases,
  type PromoteTask,
  type DatasetCase,
} from "@/lib/api";

/**
 * 待标注 Tab：promote 闸门队列。
 *
 * 后台调度器自动发现生产 trace → judge 打分 → needs_confirm 状态，
 * 本页展示这些待人工确认的 task，提供 accept/reject 操作。
 *
 * - reject：一键 confirm 后提交
 * - accept：打开 Sheet 表单，单选归入已有 case / 新建 case + 可选 reference_output
 * - judge 分数徽章取 content_overall（SD5）：≥0.8 绿 / 0.4~0.8 黄 / <0.4 红 / null 灰
 * - 5s 轮询（对齐 tests.tsx），切走 Tab 时自动停止（Radix Tabs 卸载）
 */
export default function ReviewTab() {
  const navigate = useNavigate();
  const [tasks, setTasks] = useState<PromoteTask[]>([]);
  const [loading, setLoading] = useState(true);

  // accept 表单 Sheet
  const [acceptTask, setAcceptTask] = useState<PromoteTask | null>(null);
  const [acceptOpen, setAcceptOpen] = useState(false);
  const [growingCases, setGrowingCases] = useState<DatasetCase[]>([]);
  const [submitting, setSubmitting] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const resp = await getPromoteTasks({ status: "needs_confirm", page: 1, page_size: 50 }).catch(() => ({
        tasks: [],
        total: 0,
        page: 1,
        page_size: 50,
      }));
      setTasks(resp.tasks);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取待标注列表失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  // ── reject：一键确认 ──
  async function handleReject(task: PromoteTask) {
    if (!confirm(`确认拒绝 ${task.trace_id.slice(0, 16)}？该 trace 不会入数据集。`)) return;
    try {
      await decidePromoteTask(task.task_id, { decision: "reject" });
      toast.success("已拒绝");
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "操作失败");
    }
  }

  // ── accept：打开表单 Sheet ──
  async function openAcceptForm(task: PromoteTask) {
    setAcceptTask(task);
    setAcceptOpen(true);
    // 拉取 growing 现有 case 供「归入」下拉
    try {
      const cs = await getDatasetCases("growing");
      setGrowingCases(cs.cases);
    } catch {
      setGrowingCases([]);
    }
  }

  return (
    <div className="tab-pane">
      {loading ? (
        <div className="page-loading">加载待标注列表…</div>
      ) : tasks.length === 0 ? (
        <div className="monitor-empty">暂无待标注任务（后台调度器 5 分钟扫描一次自动发现）</div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Trace</th>
              <th>Judge 分数</th>
              <th>判定</th>
              <th>状态</th>
              <th>创建时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((t) => {
              const overall = t.judge_scores?.content_overall;
              const badgeClass = judgeBadgeClass(overall);
              const badgeText = overall != null ? overall.toFixed(2) : "未评";
              return (
                <tr key={t.task_id}>
                  <td>
                    <button
                      className="action-link mono"
                      onClick={() => navigate(`/traces/${t.trace_id}`)}
                      title="查看 trace 详情"
                    >
                      {t.trace_id.slice(0, 16)}
                    </button>
                  </td>
                  <td>
                    <span className={`judge-badge ${badgeClass}`}>{badgeText}</span>
                    {t.judge_scores?.is_badcase && (
                      <span className="judge-badge badcase" title="badcase">⚠</span>
                    )}
                  </td>
                  <td>{verdictLabel(t.judge_verdict)}</td>
                  <td>
                    <span className={`session-status ${t.status}`}>{statusLabel(t.status)}</span>
                  </td>
                  <td>{t.created_at?.slice(0, 19).replace("T", " ") || "—"}</td>
                  <td className="test-actions">
                    <button className="action-link" onClick={() => openAcceptForm(t)}>收</button>
                    <button className="action-link danger" onClick={() => handleReject(t)}>拒</button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <AcceptFormSheet
        task={acceptTask}
        open={acceptOpen}
        onOpenChange={setAcceptOpen}
        growingCases={growingCases}
        submitting={submitting}
        setSubmitting={setSubmitting}
        onDone={refresh}
      />
    </div>
  );
}

// ── accept 表单 Sheet ──

function AcceptFormSheet({
  task,
  open,
  onOpenChange,
  growingCases,
  submitting,
  setSubmitting,
  onDone,
}: {
  task: PromoteTask | null;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  growingCases: DatasetCase[];
  submitting: boolean;
  setSubmitting: (v: boolean) => void;
  onDone: () => void;
}) {
  // 表单状态
  const [mode, setMode] = useState<"existing" | "new">("new");
  const [targetCaseId, setTargetCaseId] = useState("");
  const [newTitle, setNewTitle] = useState("");
  const [demandMd, setDemandMd] = useState("");
  const [reference, setReference] = useState("");

  // 每次打开重置
  useEffect(() => {
    if (open) {
      setMode(growingCases.length > 0 ? "existing" : "new");
      setTargetCaseId(growingCases[0]?.case_id || "");
      setNewTitle("");
      setDemandMd("");
      setReference("");
    }
  }, [open, growingCases]);

  async function handleSubmit() {
    if (!task) return;
    setSubmitting(true);
    try {
      const payload: Parameters<typeof decidePromoteTask>[1] = { decision: "accept" };
      if (mode === "existing") {
        if (!targetCaseId) {
          toast.error("请选择目标 case");
          setSubmitting(false);
          return;
        }
        payload.target_case_id = targetCaseId;
      } else {
        if (!newTitle.trim()) {
          toast.error("请填写新 case 标题");
          setSubmitting(false);
          return;
        }
        payload.new_case_title = newTitle.trim();
        payload.demand_md = demandMd;
      }
      if (reference.trim()) {
        payload.reference_output = reference;
      }
      const resp = await decidePromoteTask(task.task_id, payload);
      toast.success(`已收入 growing（case ${resp.case_id?.slice(0, 12) || "?"}）`);
      onOpenChange(false);
      onDone();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "操作失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right">
        <SheetHeader>
          <SheetTitle>收入 Growing</SheetTitle>
          <SheetClose asChild>
            <button className="sheet-close-x" aria-label="关闭">✕</button>
          </SheetClose>
        </SheetHeader>

        <div className="sheet-body">
          {task && (
            <div className="accept-task-info">
              <span>来源 Trace：<span className="mono">{task.trace_id.slice(0, 16)}</span></span>
              <span>
                Judge：<span className={`judge-badge ${judgeBadgeClass(task.judge_scores?.content_overall)}`}>
                  {task.judge_scores?.content_overall?.toFixed(2) ?? "未评"}
                </span>
              </span>
            </div>
          )}

          {/* 归入模式选择 */}
          <section className="accept-mode">
            <label className={`accept-mode-opt ${mode === "existing" ? "active" : ""}`}>
              <input
                type="radio"
                checked={mode === "existing"}
                onChange={() => setMode("existing")}
                disabled={growingCases.length === 0}
              />
              <span>归入已有 case</span>
            </label>
            <label className={`accept-mode-opt ${mode === "new" ? "active" : ""}`}>
              <input
                type="radio"
                checked={mode === "new"}
                onChange={() => setMode("new")}
              />
              <span>新建 case</span>
            </label>
          </section>

          {mode === "existing" ? (
            <section className="accept-form-section">
              <label className="test-field">
                <span>目标 Case</span>
                <select
                  className="evolve-select"
                  value={targetCaseId}
                  onChange={(e) => setTargetCaseId(e.target.value)}
                >
                  {growingCases.length === 0 && <option value="">（暂无 growing case）</option>}
                  {growingCases.map((c) => (
                    <option key={c.case_id} value={c.case_id}>
                      {c.case_id} — {c.title}
                    </option>
                  ))}
                </select>
              </label>
            </section>
          ) : (
            <section className="accept-form-section">
              <label className="test-field">
                <span>新 Case 标题</span>
                <input
                  className="config-input"
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                  placeholder="如：都市言情-001"
                />
              </label>
              <label className="test-field">
                <span>Demand（创作需求，规范化文本）</span>
                <textarea
                  className="config-textarea"
                  value={demandMd}
                  onChange={(e) => setDemandMd(e.target.value)}
                  placeholder="标注者规范化后的创作需求…"
                  rows={8}
                />
              </label>
            </section>
          )}

          {/* reference_output（可选）*/}
          <section className="accept-form-section">
            <label className="test-field">
              <span>编辑终稿 reference_output（可选）</span>
              <textarea
                className="config-textarea"
                value={reference}
                onChange={(e) => setReference(e.target.value)}
                placeholder="留空则后端自动从 trace 的 user_edit 事件提取"
                rows={6}
              />
            </label>
          </section>

          <section className="case-actions">
            <button
              className="config-button primary"
              onClick={handleSubmit}
              disabled={submitting}
            >
              {submitting ? "提交中…" : "确认收入"}
            </button>
          </section>
        </div>
      </SheetContent>
    </Sheet>
  );
}

// ── 辅助函数 ──

/** judge 分数徽章 class（SD5 阈值）。 */
function judgeBadgeClass(overall: number | undefined | null): string {
  if (overall == null) return "none";
  if (overall >= 0.8) return "high";
  if (overall >= 0.4) return "mid";
  return "low";
}

function verdictLabel(v: string | null): string {
  const map: Record<string, string> = {
    auto_promote: "自动推荐",
    needs_human: "需人工",
    auto_reject: "自动拒绝",
  };
  return v ? (map[v] ?? v) : "—";
}

function statusLabel(s: string): string {
  const map: Record<string, string> = {
    pending: "待 judge",
    judging: "评判中",
    needs_confirm: "待确认",
    rejected: "已拒绝",
    promoted: "已入库",
  };
  return map[s] ?? s;
}
