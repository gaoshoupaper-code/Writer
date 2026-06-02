import ReactMarkdown from "react-markdown";

type DetailOutlinePanelProps = {
  markdown: string;
  fileCount: number;
  loading: boolean;
};

export function DetailOutlinePanel({ markdown, fileCount, loading }: DetailOutlinePanelProps) {
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
        <div className="outline-markdown script-markdown">
          {markdown.trim() ? (
            <ReactMarkdown>{markdown}</ReactMarkdown>
          ) : (
            <p>工作目录中的细纲内容会在这里显示。</p>
          )}
        </div>
      </div>
    </section>
  );
}
