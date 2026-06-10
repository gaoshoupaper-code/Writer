import ReactMarkdown from "react-markdown";
import type { VolumeChapter } from "../../lib/types";

type OutlineTab = "outline" | "volume";

type ScriptPanelProps = {
  workspacePath?: string;
  outlineMarkdown: string;
  volumeChapters: VolumeChapter[];
  activeVolumeFilename: string;
  loading: boolean;
  activeTab: OutlineTab;
  onTabChange: (tab: OutlineTab) => void;
  onSelectVolume: (filename: string) => void;
};

export function ScriptPanel({
  workspacePath,
  outlineMarkdown,
  volumeChapters,
  activeVolumeFilename,
  loading,
  activeTab,
  onTabChange,
  onSelectVolume,
}: ScriptPanelProps) {
  const activeVolume = volumeChapters.find((ch) => ch.filename === activeVolumeFilename) ?? volumeChapters[0];

  return (
    <section className="panel-surface content-panel" aria-label="大纲">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">Script</span>
          <h2>大纲</h2>
        </div>
        <div className="panel-heading-actions">
          {loading ? <span className="outline-state">加载中</span> : null}
          <div className="outline-sub-tabs">
            <button
              className={`outline-sub-tab${activeTab === "outline" ? " active" : ""}`}
              type="button"
              onClick={() => onTabChange("outline")}
            >
              总纲
            </button>
            <button
              className={`outline-sub-tab${activeTab === "volume" ? " active" : ""}`}
              type="button"
              onClick={() => onTabChange("volume")}
            >
              卷纲{volumeChapters.length > 0 ? ` (${volumeChapters.length})` : ""}
            </button>
          </div>
        </div>
      </div>

      <div className="content-panel-body">
        {activeTab === "outline" ? (
          <>
            <div className="workspace-card compact-card">
              <span className="field-label">后端工作目录</span>
              <p className="workspace-path">{workspacePath ?? "选择或新建工作目录后显示对应目录"}</p>
            </div>

            <div className="outline-markdown script-markdown">
              {outlineMarkdown.trim() ? <ReactMarkdown>{outlineMarkdown}</ReactMarkdown> : <p>工作目录中的大纲内容会在这里显示。</p>}
            </div>
          </>
        ) : volumeChapters.length > 0 ? (
          <div className="detail-outline-layout">
            <aside className="detail-outline-sidebar" aria-label="卷纲导航">
              <span className="field-label">卷纲</span>
              <div className="detail-outline-list">
                {volumeChapters.map((ch) => (
                  <button
                    className={`detail-outline-list-item${ch.filename === activeVolume?.filename ? " active" : ""}`}
                    key={ch.filename}
                    type="button"
                    onClick={() => onSelectVolume(ch.filename)}
                  >
                    <span>{ch.title}</span>
                  </button>
                ))}
              </div>
            </aside>

            <article className="outline-markdown detail-outline-markdown">
              {activeVolume?.markdown.trim() ? <ReactMarkdown>{activeVolume.markdown}</ReactMarkdown> : <p>该卷暂无内容。</p>}
            </article>
          </div>
        ) : (
          <div className="empty-state">
            <span className="placeholder-mark">暂无卷纲</span>
            <h3>当前工作目录中暂无卷纲文件</h3>
            <p>在 volume 文件夹中的 Markdown 文件会在这里显示。</p>
          </div>
        )}
      </div>
    </section>
  );
}
