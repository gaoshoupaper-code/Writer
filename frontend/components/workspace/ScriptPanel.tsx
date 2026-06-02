import ReactMarkdown from "react-markdown";

type ScriptPanelProps = {
  workspacePath?: string;
  markdown: string;
  loading: boolean;
};

export function ScriptPanel({ workspacePath, markdown, loading }: ScriptPanelProps) {
  return (
    <section className="panel-surface content-panel" aria-label="剧本内容">
      <div className="panel-heading">
        <div>
          <span className="section-kicker">Script</span>
          <h2>剧本内容</h2>
        </div>
        {loading ? <span className="outline-state">加载中</span> : null}
      </div>

      <div className="content-panel-body">
        <div className="workspace-card compact-card">
          <span className="field-label">后端工作目录</span>
          <p className="workspace-path">{workspacePath ?? "选择或新建工作目录后显示对应目录"}</p>
        </div>

        <div className="outline-markdown script-markdown">
          {markdown.trim() ? <ReactMarkdown>{markdown}</ReactMarkdown> : <p>工作目录中的剧本内容会在这里显示。</p>}
        </div>
      </div>
    </section>
  );
}
