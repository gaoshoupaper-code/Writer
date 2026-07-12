import type { AgentElementView, AgentDiff } from "@/lib/api";
import { LifecycleSwimlane } from "./LifecycleSwimlane";

/**
 * Middleware Tab 容器：委托给 LifecycleSwimlane 渲染泳道图。
 *
 * 这里只做数据透传和空状态处理，泳道图逻辑全在 LifecycleSwimlane 里。
 */
export function MiddlewareTab({
  agents,
  diffs,
  hasSource,
}: {
  agents: AgentElementView[];
  diffs: Map<string, AgentDiff> | null;
  hasSource: boolean;
}) {
  const totalMW = agents.reduce((sum, a) => sum + a.middlewares.length, 0);

  if (totalMW === 0) {
    return (
      <div className="monitor-empty">该版本所有 Agent 均无 middleware。</div>
    );
  }

  return (
    <div>
      <LifecycleSwimlane agents={agents} diffs={diffs} />
      {!hasSource && (
        <div className="source-warning" style={{ marginTop: 12 }}>
          ⚠ 此版本无源码快照，middleware 用途说明（docstring）不可读取。
        </div>
      )}
    </div>
  );
}
