import type { WorkspacePanel } from "../../lib/types";

type SidebarItem = {
  id: WorkspacePanel;
  label: string;
  description: string;
};

const SIDEBAR_ITEMS: SidebarItem[] = [
  { id: "chat", label: "对话", description: "与 Agent 协作推进故事" },
  { id: "characters", label: "人物", description: "从剧本章节提取角色" },
  { id: "script", label: "大纲", description: "查看总纲与卷纲" },
  { id: "detail_outline", label: "细纲", description: "查看逐章详细规划" },
  { id: "worldview", label: "世界观", description: "查看故事世界观设定" },
  { id: "novel", label: "正文", description: "查看完整小说正文" },
  { id: "trace", label: "检测系统", description: "执行追踪与 Token 图检测" },
];

type SidebarProps = {
  activePanel: WorkspacePanel;
  onPanelChange: (panel: WorkspacePanel) => void;
};

export function Sidebar({ activePanel, onPanelChange }: SidebarProps) {
  return (
    <aside className="dashboard-sidebar" aria-label="工作台模块">
      <nav className="sidebar-nav">
        {SIDEBAR_ITEMS.map((item) => {
          const isActive = activePanel === item.id;
          return (
            <button
              className={`sidebar-item ${isActive ? "active" : ""} transition-all duration-150`}
              type="button"
              key={item.id}
              onClick={() => onPanelChange(item.id)}
            >
              <span>{item.label}</span>
              <small>{item.description}</small>
            </button>
          );
        })}
      </nav>
    </aside>
  );
}
