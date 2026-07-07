
import { useState } from "react";
import type { Style } from "../../lib/types";

const STYLE_TABS = [
  { key: "meta_style", label: "主控风格" },
  { key: "storybuilding_style", label: "故事构建风格" },
  { key: "detail_outline_style", label: "细纲风格" },
  { key: "writing_style", label: "写作风格" },
] as const;

type StyleTabKey = (typeof STYLE_TABS)[number]["key"];

type StyleModalProps = {
  styles: Style[];
  activeStyleId: string | null;
  creating: boolean;
  onCreateStyle: (name: string, metaStyle: string, storybuildingStyle: string, detailOutlineStyle: string, writingStyle: string) => Promise<void>;
  onUpdateStyle: (styleId: string, fields: Record<string, string>) => Promise<boolean>;
  onDeleteStyle: (styleId: string) => Promise<void>;
  onSelectStyle: (styleId: string | null) => void;
  onOptimizeStyle: (styleType: string, content: string) => Promise<string>;
  onClose: () => void;
};

export function StyleModal({
  styles,
  activeStyleId,
  creating,
  onCreateStyle,
  onUpdateStyle,
  onDeleteStyle,
  onSelectStyle,
  onOptimizeStyle,
  onClose,
}: StyleModalProps) {
  const [mode, setMode] = useState<"list" | "create">("list");
  const [activeTab, setActiveTab] = useState<StyleTabKey>("meta_style");
  const [newName, setNewName] = useState("");
  const [newFields, setNewFields] = useState({ meta_style: "", storybuilding_style: "", detail_outline_style: "", writing_style: "" });
  const [deletingId, setDeletingId] = useState("");
  const [optimizing, setOptimizing] = useState(false);
  const [saving, setSaving] = useState(false);

  // Edit state for the active style
  const [editDirty, setEditDirty] = useState<Record<string, string>>({});
  const [editDirtyName, setEditDirtyName] = useState<string | null>(null);

  const activeStyle = styles.find((s) => s.style_id === activeStyleId);

  function getEditValue(key: StyleTabKey): string {
    if (key in editDirty) return editDirty[key];
    return activeStyle?.[key] ?? "";
  }

  function getEditName(): string {
    return editDirtyName ?? activeStyle?.name ?? "";
  }

  async function handleCreate() {
    const name = newName.trim();
    if (!name || creating) return;
    await onCreateStyle(name, newFields.meta_style, newFields.storybuilding_style, newFields.detail_outline_style, newFields.writing_style);
    setNewName("");
    setNewFields({ meta_style: "", storybuilding_style: "", detail_outline_style: "", writing_style: "" });
    setMode("list");
  }

  async function handleDelete(styleId: string) {
    if (deletingId) return;
    setDeletingId(styleId);
    try {
      await onDeleteStyle(styleId);
    } finally {
      setDeletingId("");
    }
  }

  async function handleOptimize(content: string, onResult: (optimized: string) => void) {
    if (!content.trim() || optimizing) return;
    setOptimizing(true);
    try {
      const optimized = await onOptimizeStyle(activeTab, content);
      onResult(optimized);
    } finally {
      setOptimizing(false);
    }
  }

  async function handleSave() {
    if (!activeStyleId || saving) return;
    setSaving(true);
    try {
      const fields: Record<string, string> = {};
      let hasChanges = false;
      if (editDirtyName !== null && editDirtyName !== activeStyle?.name) {
        fields.name = editDirtyName;
        hasChanges = true;
      }
      for (const tab of STYLE_TABS) {
        if (tab.key in editDirty && editDirty[tab.key] !== activeStyle?.[tab.key]) {
          fields[tab.key] = editDirty[tab.key];
          hasChanges = true;
        }
      }
      if (hasChanges) {
        const ok = await onUpdateStyle(activeStyleId, fields);
        if (ok) {
          setEditDirty({});
          setEditDirtyName(null);
        }
      }
    } finally {
      setSaving(false);
    }
  }

  function hasUnsavedChanges(): boolean {
    if (!activeStyle) return false;
    if (editDirtyName !== null && editDirtyName !== activeStyle.name) return true;
    return STYLE_TABS.some((tab) => tab.key in editDirty && editDirty[tab.key] !== activeStyle[tab.key]);
  }

  function handleSelectStyle(styleId: string | null) {
    if (hasUnsavedChanges()) {
      const ok = window.confirm("当前有未保存的修改，确认切换吗？");
      if (!ok) return;
    }
    setEditDirty({});
    setEditDirtyName(null);
    onSelectStyle(styleId);
  }

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="modal-content style-modal-content"
        role="dialog"
        aria-modal="true"
        aria-labelledby="style-modal-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="style-modal-header">
          <h2 className="modal-title" id="style-modal-title">写作风格</h2>
          <button className="modal-button modal-cancel style-modal-close" type="button" onClick={onClose}>关闭</button>
        </div>

        <div className="style-modal-body">
          {/* Left sidebar */}
          <div className="style-modal-sidebar">
            <button
              className={`style-new-button${mode === "create" ? " active" : ""}`}
              type="button"
              onClick={() => setMode("create")}
            >
              + 新建风格
            </button>

            <div className="style-list">
              {styles.length === 0 ? (
                <p className="style-empty">暂无风格</p>
              ) : (
                styles.map((style) => (
                  <div
                    key={style.style_id}
                    className={`style-item${activeStyleId === style.style_id ? " active" : ""}`}
                    onClick={() => { setMode("list"); handleSelectStyle(activeStyleId === style.style_id ? null : style.style_id); }}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(event) => { if (event.key === "Enter") { setMode("list"); handleSelectStyle(activeStyleId === style.style_id ? null : style.style_id); } }}
                  >
                    <span className="style-item-name">{style.name}</span>
                    {activeStyleId === style.style_id && <span className="style-item-badge">已选</span>}
                    <button
                      className="style-item-delete"
                      type="button"
                      onClick={(event) => { event.stopPropagation(); handleDelete(style.style_id); }}
                      disabled={!!deletingId}
                      aria-label={`删除风格 ${style.name}`}
                    >
                      {deletingId === style.style_id ? "..." : "x"}
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* Right main panel */}
          <div className="style-modal-main">
            {mode === "create" ? (
              <form
                className="style-create-form"
                onSubmit={(event) => { event.preventDefault(); handleCreate(); }}
              >
                <h3 className="style-create-title">新建写作风格</h3>
                <label className="style-field-label">
                  风格名称
                  <input
                    className="thread-input style-input"
                    value={newName}
                    onChange={(event) => setNewName(event.target.value)}
                    placeholder="例如：海明威式简洁"
                    autoFocus
                    disabled={creating}
                  />
                </label>

                {/* Tabs for create mode */}
                <div className="style-tabs">
                  {STYLE_TABS.map((tab) => (
                    <button
                      key={tab.key}
                      type="button"
                      className={`style-tab${activeTab === tab.key ? " active" : ""}`}
                      onClick={() => setActiveTab(tab.key)}
                    >
                      {tab.label}
                    </button>
                  ))}
                </div>

                <label className="style-field-label">
                  {STYLE_TABS.find((t) => t.key === activeTab)?.label}
                  <textarea
                    className="chat-input style-textarea"
                    value={newFields[activeTab]}
                    onChange={(event) => setNewFields((prev) => ({ ...prev, [activeTab]: event.target.value }))}
                    placeholder="描述你想让 AI 遵循的写作风格..."
                    rows={8}
                    disabled={creating}
                  />
                </label>

                <div className="style-create-actions">
                  <button
                    className="style-optimize-button"
                    type="button"
                    onClick={() => handleOptimize(
                      newFields[activeTab],
                      (optimized) => setNewFields((prev) => ({ ...prev, [activeTab]: optimized })),
                    )}
                    disabled={optimizing || !newFields[activeTab].trim()}
                  >
                    {optimizing ? "AI 优化中..." : "AI 优化"}
                  </button>
                  <div className="style-actions-spacer" />
                  <button className="modal-button modal-cancel" type="button" onClick={() => setMode("list")} disabled={creating}>取消</button>
                  <button className="modal-button modal-primary" type="submit" disabled={creating || !newName.trim()}>创建</button>
                </div>
              </form>
            ) : activeStyle ? (
              <div className="style-detail">
                <div className="style-detail-header-row">
                  <input
                    className="style-detail-name-input"
                    value={getEditName()}
                    onChange={(event) => setEditDirtyName(event.target.value)}
                    placeholder="风格名称"
                  />
                  <div className="style-detail-actions">
                    {hasUnsavedChanges() && (
                      <button className="modal-button modal-primary style-save-button" type="button" onClick={handleSave} disabled={saving}>
                        {saving ? "保存中" : "保存"}
                      </button>
                    )}
                  </div>
                </div>

                {/* Tabs */}
                <div className="style-tabs">
                  {STYLE_TABS.map((tab) => (
                    <button
                      key={tab.key}
                      type="button"
                      className={`style-tab${activeTab === tab.key ? " active" : ""}`}
                      onClick={() => setActiveTab(tab.key)}
                    >
                      {tab.label}
                    </button>
                  ))}
                </div>

                <div className="style-tab-content">
                  <textarea
                    className="style-editor"
                    value={getEditValue(activeTab)}
                    onChange={(event) => setEditDirty((prev) => ({ ...prev, [activeTab]: event.target.value }))}
                    placeholder={`请输入${STYLE_TABS.find((t) => t.key === activeTab)?.label}描述...`}
                    rows={12}
                  />
                  <div className="style-edit-actions">
                    <button
                      className="style-optimize-button"
                      type="button"
                      onClick={() => handleOptimize(
                        getEditValue(activeTab),
                        (optimized) => setEditDirty((prev) => ({ ...prev, [activeTab]: optimized })),
                      )}
                      disabled={optimizing || !getEditValue(activeTab).trim()}
                    >
                      {optimizing ? "AI 优化中..." : "AI 优化"}
                    </button>
                  </div>
                </div>
              </div>
            ) : (
              <div className="style-detail-empty">
                <p>从左侧选择或新建一个风格</p>
                <p className="style-detail-hint">风格会影响角色、大纲、细纲、正文等所有写作环节</p>
              </div>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
