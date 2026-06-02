import type { WorkspacePanel } from "../../lib/types";

type SidebarItem = {
  id: WorkspacePanel;
  label: string;
  description: string;
};

const SIDEBAR_ITEMS: SidebarItem[] = [
  { id: "chat", label: "对话", description: "与 Agent 协作推进故事" },
  { id: "characters", label: "人物", description: "从剧本章节提取角色" },
  { id: "script", label: "大纲", description: "查看当前工作目录大纲" },
  { id: "detail_outline", label: "细纲", description: "查看逐章详细规划" },
  { id: "novel", label: "正文", description: "查看完整小说正文" },
  { id: "trace", label: "Trace", description: "查看 Agent 执行追踪" },
];

type SidebarProps = {
  activePanel: WorkspacePanel;
  onPanelChange: (panel: WorkspacePanel) => void;
};

export function Sidebar({ activePanel, onPanelChange }: SidebarProps) {
  return (
    <aside className="dashboard-sidebar" aria-label="工作台模块">
      <nav className="sidebar-nav">
        {SIDEBAR_ITEMS.map((item) => (
          <button
            className={`sidebar-item ${activePanel === item.id ? "active" : ""}`}
            type="button"
            key={item.id}
            onClick={() => onPanelChange(item.id)}
          >
            <span>{item.label}</span>
            <small>{item.description}</small>
          </button>
        ))}
      </nav>
    </aside>
  );
}
