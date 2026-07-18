import type { ToolInfo, ToolScope } from "@/lib/api";

/**
 * Tools Tab：平铺展示 tools/ 目录下的可进化 tool 文件。
 *
 * harness 的 tools/ 是全局平铺的，不存在 tool→agent 映射——每个文件的真实作用域
 * 各不相同（global/middleware/agent/memory）。本 Tab 诚实展示每个 tool + 其 scope 彩签，
 * 与 Memory Tab（按流水线阶段聚合记忆要素）互补。
 *
 * 不做 diff 高亮——整个 diff 管道当前失效（version_changes 写入层未重建），作为
 * 独立已知问题，不混入本次展示层改造。
 */

// scope → 展示文本。用 switch 做 discriminated union 收窄，安全访问 via/agent。
// 5 色区分：global 灰 / middleware 蓝 / agent 紫 / memory 橙 / unknown 红
function scopeLabel(scope: ToolScope): string {
  switch (scope.kind) {
    case "global":     return "全局注入";
    case "middleware": return `经 ${scope.via}`;
    case "agent":      return `${scope.agent} 专属`;
    case "memory":     return "记忆系统";
    case "unknown":    return "⚠ 未登记作用域";
  }
}

export function ToolsTab({ tools }: { tools: ToolInfo[] }) {
  // 空态：该版本 tools/ 目录无文件（或全部解析失败）
  if (tools.length === 0) {
    return (
      <div className="tool-empty">此版本无可进化 tool 文件</div>
    );
  }

  return (
    <div className="tool-list">
      {tools.map((tool) => (
        <div key={tool.path} className="tool-row">
          <div className="tool-row-main">
            <span className="tool-path">{tool.path}</span>
            {tool.description && (
              <span className="tool-desc">{tool.description}</span>
            )}
            {tool.load_error && (
              <span className="tool-load-error">⚠ {tool.load_error}</span>
            )}
          </div>
          <span className={`tool-scope tool-scope-${tool.scope.kind}`}>
            {scopeLabel(tool.scope)}
          </span>
        </div>
      ))}
    </div>
  );
}
