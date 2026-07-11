import { useState } from "react";
import type { ProcessorChange } from "@/lib/api";
import { fetchSource } from "@/lib/api";

/** middleware 展示元信息（AgentElementView.middlewares 的元素类型） */
interface MWInfo {
  hook: string | null;
  group: string | null;
  class_name: string | null;
  params: Record<string, any>;
  source_path: string | null;
}

/**
 * 泳道格子里的单个 middleware 小卡片。
 *
 * - 默认显示 class_name + group，按 diff 着色（D13）
 * - 点击展开详情：params + class_name + （modified 时）新旧对比（D11）
 * - 可懒加载源码：hasSource && source_path 时显示"查看源码"（D11/D19）
 *   加载失败显示错误占位，不阻断其他节点（D19）
 */
export function MiddlewareNode({
  mw,
  change,
  version,
  hasSource,
}: {
  mw: MWInfo;
  change?: ProcessorChange;
  version: number;
  hasSource: boolean;
}) {
  const [expanded, setExpanded] = useState(false);

  // diff 语义 → CSS 类
  const diffClass =
    change?.change_type === "added"
      ? "diff-add"
      : change?.change_type === "removed"
        ? "diff-del"
        : change?.change_type === "modified"
          ? "diff-mod"
          : "";

  return (
    <div className={`mw-node ${diffClass}`} onClick={() => setExpanded(!expanded)}>
      <div className="mw-node-class">{mw.class_name || "（未知类）"}</div>
      <div className="mw-node-group">{mw.group || "—"}</div>

      {expanded && (
        <div className="mw-detail" onClick={(e) => e.stopPropagation()}>
          {/* class_name */}
          {mw.class_name && (
            <div className="mw-detail-row">
              <span className="mw-detail-label">类：</span>
              <span className="mw-detail-val">{mw.class_name}</span>
            </div>
          )}

          {/* modified：展示 class 变更 */}
          {change?.change_type === "modified" && change.class_change.old !== change.class_change.new && (
            <div className="mw-detail-row">
              <span className="mw-detail-label">类变更：</span>
              <span className="mw-detail-val diff-del">{change.class_change.old || "—"}</span>
              {" → "}
              <span className="mw-detail-val diff-add">{change.class_change.new || "—"}</span>
            </div>
          )}

          {/* params：modified 时对比，否则直接展示 */}
          {change?.change_type === "modified" ? (
            <>
              <div className="mw-detail-row">
                <span className="mw-detail-label">旧参数：</span>
                <span className="mw-detail-val diff-del">
                  {JSON.stringify(change.params_change.old ?? {}, null, 2)}
                </span>
              </div>
              <div className="mw-detail-row">
                <span className="mw-detail-label">新参数：</span>
                <span className="mw-detail-val diff-add">
                  {JSON.stringify(change.params_change.new ?? {}, null, 2)}
                </span>
              </div>
            </>
          ) : (
            Object.keys(mw.params).length > 0 && (
              <div className="mw-detail-row">
                <span className="mw-detail-label">参数：</span>
                <span className="mw-detail-val">{JSON.stringify(mw.params, null, 2)}</span>
              </div>
            )
          )}

          {/* 源码懒加载 */}
          {hasSource && mw.source_path && (
            <MiddlewareSource version={version} path={mw.source_path} />
          )}
        </div>
      )}
    </div>
  );
}

/** 源码懒加载折叠块：点击"查看源码" → fetchSource，失败显示错误占位 */
function MiddlewareSource({ version, path }: { version: number; path: string }) {
  const [state, setState] = useState<"idle" | "loading" | "loaded" | "error">("idle");
  const [content, setContent] = useState("");
  const [error, setError] = useState("");

  const load = async () => {
    setState("loading");
    try {
      const resp = await fetchSource(version, path);
      setContent(resp.content);
      setState("loaded");
    } catch (err) {
      setError(err instanceof Error ? err.message : "未知错误");
      setState("error");
    }
  };

  if (state === "idle") {
    return (
      <div className="mw-source-toggle" onClick={load}>
        📄 查看源码
      </div>
    );
  }

  if (state === "loading") {
    return <div className="mw-source-toggle">加载中…</div>;
  }

  if (state === "error") {
    return <div className="mw-source-error">⚠ 源码加载失败：{error}</div>;
  }

  return <pre className="mw-source-block">{content}</pre>;
}
