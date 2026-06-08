import ReactMarkdown from "react-markdown";
import type { DetailOutlineChapter } from "../../lib/types";

type DetailOutlinePanelProps = {
  chapters: DetailOutlineChapter[];
  activeFilename: string;
  loading: boolean;
  onSelectChapter: (filename: string) => void;
};

export function DetailOutlinePanel({ chapters, activeFilename, loading, onSelectChapter }: DetailOutlinePanelProps) {
  const activeChapter = chapters.find((ch) => ch.filename === activeFilename) ?? chapters[0];

  return (
    <section className="panel-surface content-panel" aria-label="细纲内容">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">Detail Outline</span>
          <h2>细纲</h2>
        </div>
        {loading ? <span className="outline-state">加载中</span> : null}
      </div>

      <div className="content-panel-body">
        {chapters.length ? (
          <div className="detail-outline-layout">
            <aside className="detail-outline-sidebar" aria-label="章节导航">
              <span className="field-label">章节细纲</span>
              <div className="detail-outline-list">
                {chapters.map((chapter) => (
                  <button
                    className={`detail-outline-list-item${chapter.filename === activeChapter?.filename ? " active" : ""}`}
                    key={chapter.filename}
                    type="button"
                    onClick={() => onSelectChapter(chapter.filename)}
                  >
                    <span>{chapter.title}</span>
                  </button>
                ))}
              </div>
            </aside>

            <article className="outline-markdown detail-outline-markdown">
              {activeChapter?.markdown.trim() ? (
                <ReactMarkdown>{activeChapter.markdown}</ReactMarkdown>
              ) : (
                <p>该章节暂无内容。</p>
              )}
            </article>
          </div>
        ) : (
          <div className="empty-state">
            <span className="placeholder-mark">暂无细纲</span>
            <h3>当前工作目录中暂无详细大纲</h3>
            <p>生成详细大纲后，后端工作目录的 detail 文件夹中的 Markdown 文件会在这里显示。</p>
          </div>
        )}
      </div>
    </section>
  );
}
