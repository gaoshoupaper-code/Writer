import { useEffect, useState, useCallback } from "react";
import { getDatasetCases, getGoldenRevision, type DatasetCase, type GoldenRevision } from "@/lib/api";
import CaseDetailSheet from "./CaseDetailSheet";

/**
 * Golden Tab：冻结基准层（只读）。
 *
 * 头部展示 golden revision（指纹 + locked/intact + case 数），
 * 下方 case 表格，点击行打开详情侧滑。
 *
 * 重构 2026-07-10：
 * - 移除升级能力（golden 运行时只读）
 * - 加错误状态（加载失败显示错误卡片+重试，不静默吞成空列表）
 * - revision 拉取失败显示占位，不消失
 * - 刷新受父组件 refreshSignal 驱动
 */
export default function GoldenTab({ refreshSignal }: { refreshSignal: number }) {
  const [cases, setCases] = useState<DatasetCase[]>([]);
  const [revision, setRevision] = useState<GoldenRevision | null>(null);
  const [revError, setRevError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 详情 Sheet
  const [selected, setSelected] = useState<DatasetCase | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    setRevError(false);
    // 两个请求无依赖关系，并发拉取（原串行 await 让加载时间翻倍）
    const [csResult, revResult] = await Promise.allSettled([
      getDatasetCases("golden"),
      getGoldenRevision(),
    ]);
    if (csResult.status === "fulfilled") {
      setCases(csResult.value.cases);
    } else {
      setError(csResult.reason instanceof Error ? csResult.reason.message : "读取 golden 列表失败");
    }
    // revision 独立处理：失败不影响 case 列表展示
    if (revResult.status === "fulfilled") {
      setRevision(revResult.value);
    } else {
      setRevError(true);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh, refreshSignal]);

  function openDetail(c: DatasetCase) {
    setSelected(c);
    setSheetOpen(true);
  }

  if (loading) return <div className="page-loading">加载 golden 列表…</div>;

  if (error) {
    return (
      <div className="error-card">
        <span className="error-icon">⚠</span>
        <div className="error-body">
          <div className="error-title">加载失败</div>
          <div className="error-desc">{error}</div>
        </div>
        <button className="action-link" onClick={refresh}>重试</button>
      </div>
    );
  }

  return (
    <div className="tab-pane">
      {/* golden revision 头部 */}
      {(revision || revError) && (
        <section className="golden-revision-bar">
          {revError ? (
            <div className="rev-item">
              <span className="rev-label">Revision</span>
              <span className="rev-badge danger">⚠ 加载失败</span>
            </div>
          ) : revision && (
            <>
              <div className="rev-item">
                <span className="rev-label">Revision</span>
                <span className="rev-value mono">{revision.revision?.slice(0, 12) || "—"}</span>
              </div>
              <div className="rev-item">
                <span className="rev-label">锁定</span>
                <span className={`rev-badge ${revision.locked ? "ok" : "warn"}`}>
                  {revision.locked ? "已锁定" : "未锁定"}
                </span>
              </div>
              <div className="rev-item">
                <span className="rev-label">完整性</span>
                <span className={`rev-badge ${revision.intact ? "ok" : "danger"}`}>
                  {revision.intact ? "完好" : "已篡改"}
                </span>
              </div>
              <div className="rev-item">
                <span className="rev-label">Case 数</span>
                <span className="rev-value">{revision.case_count}</span>
              </div>
            </>
          )}
        </section>
      )}

      {/* case 表格 */}
      {cases.length === 0 ? (
        <div className="monitor-empty">暂无 golden 基准 case</div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Case ID</th>
              <th>标题</th>
              <th>Revision</th>
              <th>来源 Trace</th>
              <th>参考终稿</th>
              <th>升级时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {cases.map((c) => (
              <tr key={c.case_id}>
                <td className="mono">{c.case_id}</td>
                <td>{c.title}</td>
                <td className="mono">{c.demand_revision?.slice(0, 12) || "—"}</td>
                <td className="mono">{c.source_trace_id?.slice(0, 16) || "—"}</td>
                <td>{c.has_reference ? "✓" : "—"}</td>
                <td>{c.promoted_at?.slice(0, 19).replace("T", " ") || "—"}</td>
                <td>
                  <button className="action-link" onClick={() => openDetail(c)}>详情</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <CaseDetailSheet
        caseItem={selected}
        layer="golden"
        open={sheetOpen}
        onOpenChange={setSheetOpen}
      />
    </div>
  );
}
