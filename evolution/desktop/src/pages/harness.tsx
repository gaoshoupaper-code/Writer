import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import {
  getSnapshots,
  getProductionSnapshot,
  getElements,
  type Snapshot,
  type ElementsView,
} from "@/lib/api";

/**
 * Agent 要素透视页（设计文档：核心工作区大 tab）。
 *
 * 选择一个版本快照 → 展示该版本的 Agent 内部结构：
 * - agents 列表（meta + subagents）
 * - 每个 agent 的 prompt / skills / middlewares
 * - subagent 依赖关系
 */
export default function HarnessPage() {
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [elements, setElements] = useState<ElementsView | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedAgent, setExpandedAgent] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const snaps = await getSnapshots();
      setSnapshots(snaps);
      // 默认选 production 版本
      if (snaps.length > 0 && selectedVersion === null) {
        const prod = snaps.find((s) => s.status === "production") ?? snaps[0];
        setSelectedVersion(prod.version);
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "读取版本列表失败");
    } finally {
      setLoading(false);
    }
  }, [selectedVersion]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // 加载选中版本的 elements
  useEffect(() => {
    if (selectedVersion == null) return;
    setElements(null);
    getElements(selectedVersion)
      .then(setElements)
      .catch((err) => toast.error(err instanceof Error ? err.message : "读取 Agent 要素失败"));
  }, [selectedVersion]);

  if (loading) return <div className="page-loading">加载版本列表…</div>;

  return (
    <div className="harness-page">
      <header className="page-header">
        <h1>Agent 要素</h1>
        <p className="page-desc">透视各版本 Agent 的内部结构（prompt / skills / middleware）</p>
      </header>

      {/* 版本选择 */}
      <div className="version-selector">
        <label>选择版本：</label>
        <select
          className="evolve-select"
          value={selectedVersion ?? ""}
          onChange={(e) => setSelectedVersion(Number(e.target.value))}
        >
          {snapshots.map((s) => (
            <option key={s.version} value={s.version}>
              v{s.version} {s.status === "production" ? "（生产）" : ""} — {s.change_summary?.slice(0, 40) || "无说明"}
            </option>
          ))}
        </select>
      </div>

      {elements ? (
        <div className="elements-view">
          {/* subagent 关系图 */}
          {elements.subagent_relations.length > 0 && (
            <section className="relations-section">
              <h3>编排关系</h3>
              <div className="relations">
                {elements.subagent_relations.map((r, i) => (
                  <div key={i} className="relation-item">
                    <span className="relation-from">{r.from}</span>
                    <span className="relation-arrow">→</span>
                    <span className="relation-to">{r.to}</span>
                    <span className="relation-role">{r.role}</span>
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* agents 列表 */}
          <section className="agents-section">
            <h3>Agent 结构（{elements.agents.length}）</h3>
            {elements.agents.map((agent) => (
              <div key={agent.name} className="agent-card">
                <div
                  className="agent-card-head"
                  onClick={() => setExpandedAgent(expandedAgent === agent.name ? null : agent.name)}
                >
                  <span className={`agent-kind ${agent.kind}`}>{agent.kind}</span>
                  <span className="agent-name">{agent.name}</span>
                  <span className="agent-meta">
                    {agent.skills.length} skills / {agent.middlewares.length} middleware
                  </span>
                  <span className="caret">{expandedAgent === agent.name ? "▾" : "▸"}</span>
                </div>
                {expandedAgent === agent.name && (
                  <div className="agent-card-body">
                    {/* prompt */}
                    {agent.prompt.body && (
                      <div className="agent-section">
                        <h5>Prompt</h5>
                        <pre className="agent-prompt">{agent.prompt.body.slice(0, 500)}{agent.prompt.body.length > 500 ? "…" : ""}</pre>
                      </div>
                    )}
                    {/* skills */}
                    {agent.skills.length > 0 && (
                      <div className="agent-section">
                        <h5>Skills（{agent.skills.length}）</h5>
                        {agent.skills.map((sk, i) => (
                          <div key={i} className="skill-item">
                            <code className="skill-path">{sk.path}</code>
                            {sk.load_error && <span className="skill-error">⚠ {sk.load_error}</span>}
                          </div>
                        ))}
                      </div>
                    )}
                    {/* middlewares */}
                    {agent.middlewares.length > 0 && (
                      <div className="agent-section">
                        <h5>Middlewares（{agent.middlewares.length}）</h5>
                        {agent.middlewares.map((mw, i) => (
                          <div key={i} className="mw-item">
                            <span className="mw-class">{mw.class_name || "—"}</span>
                            <span className="mw-hook">{mw.hook || "—"}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </section>

          {!elements.has_source && (
            <div className="source-warning">⚠ 此版本无源码快照（source_commit 缺失），仅显示配置信息</div>
          )}
        </div>
      ) : (
        <div className="page-loading">加载 Agent 要素…</div>
      )}
    </div>
  );
}
