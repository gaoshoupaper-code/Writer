import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { getDatasetCases, getGoldenRevision, type DatasetCase, type GoldenRevision } from "@/lib/api";
import CaseDetailSheet from "./CaseDetailSheet";

/**
 * Golden Tab：冻结基准层。
 *
 * 头部展示 golden revision（指纹 + locked/intact + case 数），
 * 下方 case 表格，点击行打开详情侧滑。
 */
export default function GoldenTab() {
  const [cases, setCases] = useState<DatasetCase[]>([]);
  const [revision, setRevision] = useState<GoldenRevision | null>(null);
  const [loading, setLoading] = useState(true);

  // 详情 Sheet
  const [selected, setSelected] = useState<DatasetCase | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [cs, rev] = await Promise.all([
        getDatasetCases("golden").catch(() => ({ cases: [], total: 0 })),
        getGoldenRevision().catch(() => null),
      ]);
      setCases(cs.cases);
      setRevision(rev);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取 golden 列表失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  function openDetail(c: DatasetCase) {
    setSelected(c);
    setSheetOpen(true);
  }

  if (loading) return <div className="page-loading">加载 golden 列表…</div>;

  return (
    <div className="tab-pane">
      {/* golden revision 头部 */}
      {revision && (
        <section className="golden-revision-bar">
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
