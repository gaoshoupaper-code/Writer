import type { Hunk } from "@/lib/api";

/**
 * Prompt 行级 diff 渲染器。
 *
 * 后端算好的 hunks 只有 equal/insert/delete 三种（replace 已被拆成 del+ins）。
 * 全文叠加高亮：equal 正常色、insert 绿底带 +、delete 红底带 - 并删除线。
 * 这让改动在完整 prompt 上下文里一目了然（D15）。
 */
export function PromptDiffViewer({ hunks }: { hunks: Hunk[] }) {
  return (
    <div className="prompt-diff-view">
      {hunks.map((hunk, hi) =>
        hunk.lines.map((line, li) => (
          <div key={`${hi}-${li}`} className={`prompt-diff-line ${hunk.type}`}>
            {line || "\u00A0" /* 空行用 nbsp 撑住高度 */}
          </div>
        )),
      )}
    </div>
  );
}
