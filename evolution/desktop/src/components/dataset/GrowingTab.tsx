import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { getDatasetCases, promoteCaseToGolden, type DatasetCase } from "@/lib/api";
import CaseDetailSheet from "./CaseDetailSheet";

/**
 * Growing Tab：增长探索层。
 *
 * 生产 promote 入库的真实 case。行内有升级按钮（快捷入口），
 * 详情侧滑内也有升级按钮。
 */
export default function GrowingTab() {
  const [cases, setCases] = useState<DatasetCase[]>([]);
  const [loading, setLoading] = useState(true);

  // 详情 Sheet
  const [selected, setSelected] = useState<DatasetCase | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [promotingId, setPromotingId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const cs = await getDatasetCases("growing").catch(() => ({ cases: [], total: 0 }));
      setCases(cs.cases);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取 growing 列表失败");
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

  async function handlePromote(c: DatasetCase) {
    if (!confirm(`确认将 ${c.case_id} 升级到 golden？此操作会重算 golden revision。`)) return;
    setPromotingId(c.case_id);
    try {
      const resp = await promoteCaseToGolden(c.case_id);
      toast.success(`已升级到 golden（revision ${resp.demand_revision.slice(0, 12)}）`);
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "升级失败");
    } finally {
      setPromotingId(null);
    }
  }

  if (loading) return <div className="page-loading">加载 growing 列表…</div>;

  return (
    <div className="tab-pane">
      {cases.length === 0 ? (
        <div className="monitor-empty">暂无 growing 探索 case（待标注 accept 后入库）</div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Case ID</th>
              <th>标题</th>
              <th>来源 Trace</th>
              <th>参考终稿</th>
              <th>入库时间</th>
              <th>创建者</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {cases.map((c) => (
              <tr key={c.case_id}>
                <td className="mono">{c.case_id}</td>
                <td>{c.title}</td>
                <td className="mono">{c.source_trace_id?.slice(0, 16) || "—"}</td>
                <td>{c.has_reference ? "✓" : "—"}</td>
                <td>{c.promoted_at?.slice(0, 19).replace("T", " ") || "—"}</td>
                <td>{c.created_by}</td>
                <td className="test-actions">
                  <button className="action-link" onClick={() => openDetail(c)}>详情</button>
                  <button
                    className="action-link"
                    onClick={() => handlePromote(c)}
                    disabled={promotingId === c.case_id}
                  >
                    {promotingId === c.case_id ? "升级中…" : "↑ Golden"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <CaseDetailSheet
        caseItem={selected}
        layer="growing"
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        onPromoted={refresh}
      />
    </div>
  );
}
