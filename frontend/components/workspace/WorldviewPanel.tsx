import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type WorldviewPanelProps = {
  workspacePath?: string;
  markdown: string;
  loading: boolean;
};

export function WorldviewPanel({ workspacePath, markdown, loading }: WorldviewPanelProps) {
  return (
    <section className="panel-surface content-panel" aria-label="世界观">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">Worldview</span>
          <h2>世界观</h2>
        </div>
        {loading ? <span className="outline-state">加载中</span> : null}
      </div>

      <div className="content-panel-body">
        <div className="workspace-card compact-card">
          <span className="field-label">后端工作目录</span>
          <p className="workspace-path">{workspacePath ?? "选择或新建工作目录后显示对应目录"}</p>
        </div>

        <div className="outline-markdown script-markdown">
          {markdown.trim() ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown> : <p>工作目录中的 worldview.md 世界观设定文件会在这里显示。</p>}
        </div>
      </div>
    </section>
  );
}
