import { useState } from "react";
import type { ProcessorChange } from "@/lib/api";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";

/** middleware 展示元信息（AgentElementView.middlewares 的元素类型） */
interface MWInfo {
  hook: string | null;
  group: string | null;
  class_name: string | null;
  params: Record<string, any>;
  source_path: string | null;
  description: string | null;
}

/**
 * 泳道格子里的单个 middleware 小卡片。
 *
 * - 默认显示 class_name + group，按 diff 着色（D13）
 * - 点击打开侧边弹窗（Sheet）：用途说明（docstring）+ 元信息 + params（modified 时新旧对比，D11）
 */
export function MiddlewareNode({
  mw,
  change,
}: {
  mw: MWInfo;
  change?: ProcessorChange;
}) {
  const [open, setOpen] = useState(false);

  // diff 语义 → CSS 类
  const diffClass =
    change?.change_type === "added"
      ? "diff-add"
      : change?.change_type === "removed"
        ? "diff-del"
        : change?.change_type === "modified"
          ? "diff-mod"
          : "";

  return (
    <>
      <div className={`mw-node ${diffClass}`} onClick={() => setOpen(true)}>
        <div className="mw-node-class">{mw.class_name || "（未知类）"}</div>
        <div className="mw-node-group">{mw.group || "—"}</div>
      </div>

      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="right">
          <SheetHeader>
            <SheetTitle>{mw.class_name || "（未知类）"}</SheetTitle>
          </SheetHeader>

          <div className="mw-sheet-body">
            {/* 元信息：hook / group */}
            <div className="mw-sheet-meta">
              <div className="mw-detail-row">
                <span className="mw-detail-label">hook：</span>
                <span className="mw-detail-val">{mw.hook || "—"}</span>
              </div>
              <div className="mw-detail-row">
                <span className="mw-detail-label">group：</span>
                <span className="mw-detail-val">{mw.group || "—"}</span>
              </div>
            </div>

            {/* 用途说明（docstring）：主要展示区 */}
            <div className="mw-sheet-section">
              <div className="mw-sheet-section-title">用途说明</div>
              {mw.description ? (
                <pre className="mw-sheet-desc">{mw.description}</pre>
              ) : (
                <p className="text-dim mw-sheet-empty">该 middleware 无 docstring 说明。</p>
              )}
            </div>

            {/* params：modified 时对比，否则直接展示 */}
            <div className="mw-sheet-section">
              <div className="mw-sheet-section-title">参数</div>
              {change?.change_type === "modified" ? (
                <>
                  <div className="mw-detail-row">
                    <span className="mw-detail-label">旧参数：</span>
                    <span className="mw-detail-val diff-del">
                      {JSON.stringify(change.params_change.old ?? {}, null, 2)}
                    </span>
                  </div>
                  <div className="mw-detail-row">
                    <span className="mw-detail-label">新参数：</span>
                    <span className="mw-detail-val diff-add">
                      {JSON.stringify(change.params_change.new ?? {}, null, 2)}
                    </span>
                  </div>
                </>
              ) : Object.keys(mw.params).length > 0 ? (
                <div className="mw-detail-row">
                  <span className="mw-detail-val">{JSON.stringify(mw.params, null, 2)}</span>
                </div>
              ) : (
                <p className="text-dim mw-sheet-empty">无参数。</p>
              )}
            </div>

            {/* modified：展示 class 变更 */}
            {change?.change_type === "modified" &&
              change.class_change.old !== change.class_change.new && (
                <div className="mw-sheet-section">
                  <div className="mw-sheet-section-title">类变更</div>
                  <div className="mw-detail-row">
                    <span className="mw-detail-val diff-del">{change.class_change.old || "—"}</span>
                    {" → "}
                    <span className="mw-detail-val diff-add">{change.class_change.new || "—"}</span>
                  </div>
                </div>
              )}
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
