import { useEffect, useState } from "react";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetClose,
} from "@/components/ui/sheet";
import { toast } from "sonner";
import { getCaseContent, type DatasetCase } from "@/lib/api";

/**
 * Case 详情侧滑面板（SD3/SD4）。
 *
 * Golden + Growing 共用。打开时按需加载 demand.md / reference.md 内容。
 * 重构 2026-07-10：移除升级到 golden 的能力（golden 运行时只读）。
 */
export default function CaseDetailSheet({
  caseItem,
  layer,
  open,
  onOpenChange,
}: {
  caseItem: DatasetCase | null;
  layer: "golden" | "growing";
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const [content, setContent] = useState<Awaited<ReturnType<typeof getCaseContent>> | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || !caseItem) {
      setContent(null);
      return;
    }
    setLoading(true);
    getCaseContent(caseItem.case_id, layer)
      .then(setContent)
      .catch((err) => {
        toast.error(err instanceof Error ? err.message : "读取 case 内容失败");
      })
      .finally(() => setLoading(false));
  }, [open, caseItem, layer]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right">
        <SheetHeader>
          <SheetTitle>{caseItem?.title || caseItem?.case_id || "Case 详情"}</SheetTitle>
          <SheetClose asChild>
            <button className="sheet-close-x" aria-label="关闭">✕</button>
          </SheetClose>
        </SheetHeader>

        <div className="sheet-body">
          {loading && <div className="sheet-loading">加载中…</div>}

          {!loading && content && (
            <>
              {/* 元数据 */}
              <section className="case-meta">
                <div className="meta-row"><span className="meta-key">Case ID</span><span className="meta-val">{content.case_id}</span></div>
                <div className="meta-row"><span className="meta-key">层级</span><span className="meta-val">{content.layer}</span></div>
                {content.demand_revision && (
                  <div className="meta-row"><span className="meta-key">Revision</span><span className="meta-val mono">{content.demand_revision.slice(0, 12)}</span></div>
                )}
                {content.source_trace_id && (
                  <div className="meta-row"><span className="meta-key">来源 Trace</span><span className="meta-val mono">{content.source_trace_id.slice(0, 16)}</span></div>
                )}
                {content.promoted_at && (
                  <div className="meta-row"><span className="meta-key">升级时间</span><span className="meta-val">{content.promoted_at.slice(0, 19).replace("T", " ")}</span></div>
                )}
                <div className="meta-row"><span className="meta-key">创建者</span><span className="meta-val">{content.created_by}</span></div>
                <div className="meta-row"><span className="meta-key">状态</span><span className="meta-val">{content.status}</span></div>
              </section>

              {/* demand.md */}
              <section className="case-section">
                <h4 className="case-section-title">demand.md</h4>
                <pre className="case-md">{content.demand_md}</pre>
              </section>

              {/* reference.md */}
              {content.reference_md && (
                <section className="case-section">
                  <h4 className="case-section-title">reference.md（编辑终稿）</h4>
                  <pre className="case-md">{content.reference_md}</pre>
                </section>
              )}
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
