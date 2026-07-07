import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useState } from "react";
import type { NovelChapter } from "../../lib/types";

type NovelPanelProps = {
  chapters: NovelChapter[];
  activeFilename: string;
  loading: boolean;
  onSelectChapter: (filename: string) => void;
  exportUrl?: string;
  wordExportUrl?: string;
  /** 复制内容回调（数据闭环 E3：复制埋点，传当前章节文本） */
  onCopyContent?: (text: string) => void;
};

export function NovelPanel({
  chapters,
  activeFilename,
  loading,
  onSelectChapter,
  exportUrl,
  wordExportUrl,
  onCopyContent,
}: NovelPanelProps) {
  const [copied, setCopied] = useState(false);
  const activeChapter = chapters.find((ch) => ch.filename === activeFilename) ?? chapters[0];
  const hasContent = chapters.some((ch) => ch.markdown.trim().length > 0);

  const handleCopy = async () => {
    const text = activeChapter?.markdown ?? "";
    if (!text.trim()) return;
    try {
      await navigator.clipboard.writeText(text);
      onCopyContent?.(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // 剪贴板 API 不可用（HTTP 环境或权限拒绝），静默降级
    }
  };

  return (
    <section className="panel-surface content-panel" aria-label="小说正文">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">Novel</span>
          <h2>小说正文</h2>
        </div>
        <div className="panel-heading-actions">
          {loading ? <span className="outline-state">加载中</span> : null}
          {activeChapter?.markdown.trim() && onCopyContent ? (
            <button
              type="button"
              className={`export-button ${copied ? "copied" : ""}`}
              onClick={handleCopy}
              aria-label="复制本章内容"
            >
              {copied ? "已复制" : "复制"}
            </button>
          ) : null}
          <a
            className={`export-button ${hasContent && exportUrl ? "" : "disabled"}`}
            href={hasContent && exportUrl ? exportUrl : undefined}
            aria-disabled={!hasContent || !exportUrl}
          >
            导出 PDF
          </a>
          <a
            className={`export-button ${hasContent && wordExportUrl ? "" : "disabled"}`}
            href={hasContent && wordExportUrl ? wordExportUrl : undefined}
            aria-disabled={!hasContent || !wordExportUrl}
          >
            导出 Word
          </a>
        </div>
      </div>

      <div className="content-panel-body">
        {chapters.length ? (
          <div className="novel-layout">
            <aside className="novel-sidebar" aria-label="章节导航">
              <span className="field-label">章节正文</span>
              <div className="novel-list">
                {chapters.map((chapter) => (
                  <button
                    className={`novel-list-item${chapter.filename === activeChapter?.filename ? " active" : ""}`}
                    key={chapter.filename}
                    type="button"
                    onClick={() => onSelectChapter(chapter.filename)}
                  >
                    <span>{chapter.title}</span>
                  </button>
                ))}
              </div>
            </aside>

            <article className="outline-markdown novel-detail-markdown">
              {activeChapter?.markdown.trim() ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{activeChapter.markdown}</ReactMarkdown>
              ) : (
                <p>该章节暂无内容。</p>
              )}
            </article>
          </div>
        ) : (
          <div className="empty-state">
            <span className="placeholder-mark">暂无正文</span>
            <h3>当前工作目录中暂无章节正文</h3>
            <p>生成章节后，后端工作目录的 chapter 文件夹中的 Markdown 文件会在这里显示。</p>
          </div>
        )}
      </div>
    </section>
  );
}
