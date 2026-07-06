"use client";

/**
 * Middleware 源码折叠块（懒加载 + 语法高亮）。
 *
 * 交互：默认折叠，只显示元信息（类名/hook/group/params）。
 * 点击展开 → 首次展开才 fetchSource 拉源码（懒加载），后续展开用缓存。
 * hasSource=false（source_commit 缺失）时禁用展开。
 *
 * 高亮：react-syntax-highlighter 的 Prism（light 主题，python）。
 * 按需 import 只注册 python，避免全量语言包膨胀体积。
 */
import { useCallback, useState } from "react";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import type { MiddlewareInfo } from "@/lib/harness-types";
import { fetchSource } from "@/lib/harness-api";

// 按需注册语言（避免 Prism 全量语言包膨胀体积，设计文档 D3）。
// 目前要素页只展示 Python 源码（middleware .py）。
SyntaxHighlighter.registerLanguage("python", python);

interface Props {
  mw: MiddlewareInfo;
  version: number;
  hasSource: boolean;
}

interface SourceState {
  loading: boolean;
  content: string | null;
  error: string | null;
}

export function MiddlewareSource({ mw, version, hasSource }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [source, setSource] = useState<SourceState>({
    loading: false,
    content: null,
    error: null,
  });

  const handleExpand = useCallback(async () => {
    // 折叠态：直接展开
    if (!expanded) {
      setExpanded(true);
      // 已有内容（缓存）或无源码，不重复拉
      if (source.content != null || !hasSource || mw.source_path == null) return;
      setSource({ loading: true, content: null, error: null });
      try {
        const resp = await fetchSource(version, mw.source_path);
        setSource({ loading: false, content: resp.content, error: null });
      } catch (e) {
        setSource({
          loading: false,
          content: null,
          error: e instanceof Error ? e.message : "拉取源码失败",
        });
      }
    } else {
      setExpanded(false);
    }
  }, [expanded, source.content, hasSource, mw.source_path, version]);

  const params = Object.keys(mw.params).length > 0 ? mw.params : null;
  const canExpand = hasSource && mw.source_path != null;

  return (
    <div className={`harness-mw-item ${expanded ? "expanded" : ""}`}>
      <button
        className="harness-mw-head"
        onClick={handleExpand}
        disabled={!canExpand}
        style={!canExpand ? { cursor: "not-allowed", opacity: 0.6 } : undefined}
      >
        <span className="harness-mw-toggle mono">
          {canExpand ? (expanded ? "▼" : "▶") : "·"}
        </span>
        <span className="harness-mw-class mono">{mw.class_name}</span>
        <span className="harness-mw-hook">{mw.hook}</span>
        <span className="harness-mw-group mono text-mute">:{mw.group}</span>
        {params && (
          <span className="harness-mw-params mono text-mute">
            {JSON.stringify(params)}
          </span>
        )}
      </button>

      {expanded && (
        <div className="harness-mw-body">
          {source.loading && (
            <div className="text-mute" style={{ padding: 12, fontSize: 13 }}>
              加载源码…
            </div>
          )}
          {source.error && (
            <div className="error-box" style={{ margin: "8px 12px" }}>
              {source.error}
            </div>
          )}
          {source.content != null && (
            <>
              <div className="harness-mw-path mono text-mute">
                {mw.source_path}
              </div>
              <SyntaxHighlighter
                language="python"
                style={oneLight}
                customStyle={{
                  margin: 0,
                  borderRadius: 8,
                  fontSize: 12.5,
                  background: "var(--surface-2)",
                }}
                codeTagProps={{ style: { fontFamily: "var(--font-jetbrains), monospace" } }}
              >
                {source.content}
              </SyntaxHighlighter>
            </>
          )}
          {!hasSource && (
            <p className="text-dim" style={{ padding: 12, fontSize: 13 }}>
              该版本无 source_commit，无法读取源码。
            </p>
          )}
        </div>
      )}
    </div>
  );
}
