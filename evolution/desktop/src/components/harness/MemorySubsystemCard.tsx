import type { MemoryElementView, MemoryFileRole } from "@/lib/api";

/**
 * 记忆子系统视图（Memory Tab body）。
 *
 * 记忆要素物理上散落在 prompts/middleware/tools 三个目录，但语义上是一条协同链：
 *   抽取(extract) → 存储(store) → 检索(retrieve) → 回填(recall)
 * 本组件把它们按协同链阶段横向排成流水线，一眼看清"记忆系统怎么运转"。
 *
 * 数据来自独立接口 GET /harness-elements/memory（不属任何 agent，与 HarnessElementsView 分离）。
 * 老版本无 NWM 重构时 elements 为空，渲染"此版本无记忆子系统"提示。
 *
 * 原 MemorySubsystemCard 顶部独立卡片形态已降级为 Tab body（保留流水线视图 + 空态）。
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

export function MemorySubsystemCard({ elements }: { elements: MemoryElementView[] }) {
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
                    <div key={el.path} className="memory-file">
                      <span className="memory-file-name" title={el.path}>{el.name}</span>
                      <span className="memory-file-path">{el.path}</span>
                      <span className="memory-file-desc">{el.description}</span>
                    </div>
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
