import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { NovelChapter } from "../../lib/types";

type NovelPanelProps = {
  chapters: NovelChapter[];
  activeFilename: string;
  loading: boolean;
  onSelectChapter: (filename: string) => void;
  exportUrl?: string;
  wordExportUrl?: string;
};

export function NovelPanel({
  chapters,
  activeFilename,
  loading,
  onSelectChapter,
  exportUrl,
  wordExportUrl,
}: NovelPanelProps) {
  const activeChapter = chapters.find((ch) => ch.filename === activeFilename) ?? chapters[0];
  const hasContent = chapters.some((ch) => ch.markdown.trim().length > 0);

  return (
    <section className="panel-surface content-panel" aria-label="小说正文">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">Novel</span>
          <h2>小说正文</h2>
        </div>
        <div className="panel-heading-actions">
          {loading ? <span className="outline-state">加载中</span> : null}
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
