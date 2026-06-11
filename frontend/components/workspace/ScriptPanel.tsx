import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { StorylineEntry } from "../../lib/types";

type ScriptPanelProps = {
  loading: boolean;
  storylineMarkdown: string;
  storylineEntries: StorylineEntry[];
  activeStorylineFilename: string;
  onSelectStoryline: (filename: string) => void;
};

// 故事核心（storyline.md 索引）作为列表首项，与各故事线统一进同一列表+详情布局，
// 避免顶部一整段索引与下方故事线详情堆叠在一起。
const CORE_FILENAME = "__core__";

type StorylineItem = { filename: string; title: string; markdown: string };

export function ScriptPanel({
  loading,
  storylineMarkdown,
  storylineEntries,
  activeStorylineFilename,
  onSelectStoryline,
}: ScriptPanelProps) {
  const items: StorylineItem[] = [
    ...(storylineMarkdown.trim()
      ? [{ filename: CORE_FILENAME, title: "故事核心", markdown: storylineMarkdown }]
      : []),
    ...storylineEntries,
  ];
  const active = items.find((e) => e.filename === activeStorylineFilename) ?? items[0];

  return (
    <section className="panel-surface content-panel" aria-label="线纲">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">Script</span>
          <h2>线纲</h2>
        </div>
        <div className="panel-heading-actions">
          {loading ? <span className="outline-state">加载中</span> : null}
        </div>
      </div>

      <div className="content-panel-body">
        {items.length > 0 ? (
          <div className="detail-outline-layout">
            <aside className="detail-outline-sidebar" aria-label="故事线导航">
              <span className="field-label">故事线</span>
              <div className="detail-outline-list">
                {items.map((entry) => (
                  <button
                    className={`detail-outline-list-item${entry.filename === active?.filename ? " active" : ""}`}
                    key={entry.filename}
                    type="button"
                    onClick={() => onSelectStoryline(entry.filename)}
                  >
                    <span>{entry.title}</span>
                  </button>
                ))}
              </div>
            </aside>

            <article className="outline-markdown detail-outline-markdown">
              {active?.markdown.trim() ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{active.markdown}</ReactMarkdown>
              ) : (
                <p>该故事线暂无内容。</p>
              )}
            </article>
          </div>
        ) : (
          <div className="empty-state">
            <span className="placeholder-mark">暂无线纲</span>
            <h3>当前工作目录中暂无线纲</h3>
            <p>storyline.md 与 storyline/ 中的内容会在这里显示。</p>
          </div>
        )}
      </div>
    </section>
  );
}
