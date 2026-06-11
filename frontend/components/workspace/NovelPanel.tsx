import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type NovelPanelProps = {
  workspacePath?: string;
  markdown: string;
  loading: boolean;
  source?: string;
  chapterCount?: number;
  exportUrl?: string;
  wordExportUrl?: string;
};

export function NovelPanel({
  workspacePath,
  markdown,
  loading,
  source,
  chapterCount = 0,
  exportUrl,
  wordExportUrl,
}: NovelPanelProps) {
  const hasMarkdown = Boolean(markdown.trim());

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
            className={`export-button ${hasMarkdown && exportUrl ? "" : "disabled"}`}
            href={hasMarkdown && exportUrl ? exportUrl : undefined}
            aria-disabled={!hasMarkdown || !exportUrl}
          >
            导出 PDF
          </a>
          <a
            className={`export-button ${hasMarkdown && wordExportUrl ? "" : "disabled"}`}
            href={hasMarkdown && wordExportUrl ? wordExportUrl : undefined}
            aria-disabled={!hasMarkdown || !wordExportUrl}
          >
            导出 Word
          </a>
        </div>
      </div>

      <div className="content-panel-body">
        <div className="workspace-card compact-card">
          <span className="field-label">后端工作目录</span>
          <p className="workspace-path">{workspacePath ?? "选择或新建工作目录后显示对应目录"}</p>
          <p className="novel-source-copy">
            当前来源：{source === "chapter/" ? `chapter/（${chapterCount} 章）` : source || "未加载"}
          </p>
        </div>

        <div className="outline-markdown novel-markdown">
          {hasMarkdown ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown> : <p>工作目录中的 chapter/ 章节正文会在这里合并显示。</p>}
        </div>
      </div>
    </section>
  );
}
