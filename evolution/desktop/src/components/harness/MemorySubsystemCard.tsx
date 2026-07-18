import { useState } from "react";
import type {
  MemoryElementView,
  MemoryElementType,
  MemoryFileRole,
} from "@/lib/api";
import { getSnapshotSource } from "@/lib/api";

/**
 * 记忆子系统视图（Memory Tab body）。
 *
 * 记忆要素物理上散落在 prompts/middleware/tools 三个目录，但语义上是一条协同链：
 *   抽取(extract) → 存储(store) → 检索(retrieve) → 回填(recall)
 * 本组件把它们按协同链阶段横向排成流水线，一眼看清"记忆系统怎么运转"。
 *
 * 每个要素卡片显示：
 *   - name + path（文件名 + 相对包根路径）
 *   - type 彩签（prompt/middleware/tool）— 区分物理类型
 *   - file_role 彩签（抽取/存储/检索/回填）— 显式标协同链阶段，不必靠 stage 位置反推
 *   - description（一句话作用说明）
 *   - "查看源码" 折叠按钮 — 点击懒加载源码全文（/snapshots/{v}/source 端点）
 *
 * 数据来自独立接口 GET /harness-elements/memory（不属任何 agent，与 HarnessElementsView 分离）。
 * 老版本无 NWM 重构时 elements 为空，渲染"此版本无记忆子系统"提示。
 */

// 流水线阶段顺序 + 中文标签（与后端 MEMORY_ROLE_ORDER/MEMORY_ROLE_LABELS 对齐）
const ROLE_ORDER: MemoryFileRole[] = ["extract", "store", "retrieve", "recall"];
const ROLE_LABELS: Record<MemoryFileRole, string> = {
  extract: "抽取",
  store: "存储",
  retrieve: "检索",
  recall: "回填",
};
// 阶段间箭头提示的语义（NWM 数据流方向）
const ROLE_HINTS: Record<MemoryFileRole, string> = {
  extract: "从章节正文抽 typed records",
  store: "schema 决定抽什么",
  retrieve: "查询+JOIN+排版证据包",
  recall: "写作前注入 prompt",
};

// type 彩签文案（与后端 versioning.constants.MEMORY_FILES 的 type 取值对齐）
const TYPE_LABELS: Record<MemoryElementType, string> = {
  prompt: "prompt",
  middleware: "middleware",
  tool: "tool",
};

export function MemorySubsystemCard({
  elements,
  version,
}: {
  elements: MemoryElementView[];
  version: number;
}) {
  // 空状态：老版本无 NWM 记忆系统（Tab 始终显示，空态不隐藏 Tab）
  if (elements.length === 0) {
    return (
      <div className="memory-tab-empty">
        此版本无记忆子系统（早于 NWM 重构）
      </div>
    );
  }

  // 按 file_role 分组（已按 ROLE_ORDER 排序，但分组时仍显式排序保险）
  const grouped: Record<MemoryFileRole, MemoryElementView[]> = {
    extract: [],
    store: [],
    retrieve: [],
    recall: [],
  };
  for (const el of elements) {
    grouped[el.file_role]?.push(el);
  }

  return (
    <div className="memory-tab-body">
      <p className="memory-tab-sub">
        NWM 叙事记忆 · {elements.length} 个要素 · 抽取→存储→检索→回填协同链
      </p>

      <div className="memory-pipeline">
        {ROLE_ORDER.map((role, idx) => (
          <div key={role} className="memory-stage">
            {idx > 0 && <div className="memory-arrow" aria-hidden>→</div>}
            <div className="memory-stage-inner">
              <div className="memory-stage-title">
                <span className="memory-stage-label">{ROLE_LABELS[role]}</span>
                <span className="memory-stage-hint">{ROLE_HINTS[role]}</span>
              </div>
              <div className="memory-stage-files">
                {grouped[role].length === 0 ? (
                  <span className="memory-file memory-file-missing">（缺）</span>
                ) : (
                  grouped[role].map((el) => (
                    <MemoryFileCard key={el.path} el={el} version={version} />
                  ))
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * 单个记忆要素卡片：双彩签 + 描述 + 可折叠源码视图。
 *
 * 源码懒加载——首次点击"查看源码"才请求 /snapshots/{version}/source，
 * 避免一次性拉满 6 个文件（老版本可能根本没这些文件，404 即可）。
 * 状态机：idle → loading → loaded{content} | error{message}；再次点击折叠回 idle。
 */
function MemoryFileCard({
  el,
  version,
}: {
  el: MemoryElementView;
  version: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggleSource() {
    // 已展开 → 折叠
    if (expanded) {
      setExpanded(false);
      return;
    }
    // 展开：若已加载过直接展开，否则懒加载
    setExpanded(true);
    if (content !== null || loading) return;
    setLoading(true);
    setError(null);
    try {
      const res = await getSnapshotSource(version, el.path);
      setContent(res.content);
    } catch (err) {
      setError(err instanceof Error ? err.message : "源码加载失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="memory-file">
      <div className="memory-file-head">
        <span className="memory-file-name" title={el.path}>{el.name}</span>
        <div className="memory-file-tags">
          <span className={`memory-tag memory-tag-type memory-tag-type-${el.type}`}>
            {TYPE_LABELS[el.type]}
          </span>
          <span className={`memory-tag memory-tag-role memory-tag-role-${el.file_role}`}>
            {ROLE_LABELS[el.file_role]}
          </span>
        </div>
      </div>
      <span className="memory-file-path">{el.path}</span>
      <span className="memory-file-desc">{el.description}</span>
      <button
        type="button"
        className="memory-source-toggle"
        onClick={toggleSource}
        aria-expanded={expanded}
      >
        {expanded ? "▾ 收起源码" : "▸ 查看源码"}
      </button>
      {expanded && (
        <div className="memory-source-body">
          {loading && <div className="memory-source-loading">加载源码…</div>}
          {error && (
            <div className="memory-source-error">⚠ {error}</div>
          )}
          {!loading && !error && content !== null && (
            <pre className="memory-source-view">{content}</pre>
          )}
        </div>
      )}
    </div>
  );
}
