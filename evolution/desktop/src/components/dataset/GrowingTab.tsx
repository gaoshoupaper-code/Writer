import { useEffect, useState, useCallback } from "react";
import { getDatasetCases, type DatasetCase } from "@/lib/api";
import CaseDetailSheet from "./CaseDetailSheet";

/**
 * Growing Tab：增长探索层。
 *
 * 生产 promote 入库的真实 case。只读查看（重构 2026-07-10：
 * golden 运行时只读，运行时升级能力已移除）。
 *
 * 刷新：受父组件 refreshSignal 驱动（页面头部刷新按钮）。
 */
export default function GrowingTab({ refreshSignal }: { refreshSignal: number }) {
  const [cases, setCases] = useState<DatasetCase[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 详情 Sheet
  const [selected, setSelected] = useState<DatasetCase | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const cs = await getDatasetCases("growing");
      setCases(cs.cases);
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取 growing 列表失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh, refreshSignal]);

  function openDetail(c: DatasetCase) {
    setSelected(c);
    setSheetOpen(true);
  }

  if (loading) return <div className="page-loading">加载 growing 列表…</div>;

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
      />
    </div>
  );
}
