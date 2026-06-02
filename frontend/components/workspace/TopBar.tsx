import type { WorkspaceSummary } from "../../lib/types";

type ThemeMode = "light" | "dark";

type TopBarProps = {
  workspaces: WorkspaceSummary[];
  activeWorkspaceId: string;
  creatingWorkspace: boolean;
  deletingWorkspace: boolean;
  theme: ThemeMode;
  onWorkspaceChange: (workspaceId: string) => void;
  onCreateWorkspace: () => void;
  onRequestDeleteWorkspace: () => void;
  onThemeToggle: () => void;
};

export function TopBar({
  workspaces,
  activeWorkspaceId,
  creatingWorkspace,
  deletingWorkspace,
  theme,
  onWorkspaceChange,
  onCreateWorkspace,
  onRequestDeleteWorkspace,
  onThemeToggle,
}: TopBarProps) {
  return (
    <header className="dashboard-topbar">
      <div className="brand">
        <span className="brand-mark">W</span>
        <span>
          <strong>Writer Agent</strong>
          <small>AI writing workspace</small>
        </span>
      </div>

      <div className="workspace-switcher" aria-label="工作目录管理">
        <div className="workspace-control-row">
          <select
            className="thread-select workspace-select"
            id="workspace-select"
            value={activeWorkspaceId}
            onChange={(event) => onWorkspaceChange(event.target.value)}
          >
            <option value="">选择一个工作目录</option>
            {workspaces.map((workspace) => (
              <option key={workspace.workspace_id} value={workspace.workspace_id}>
                {workspace.outline_name}
              </option>
            ))}
          </select>
          <button className="thread-button workspace-create-button" type="button" onClick={onCreateWorkspace} disabled={creatingWorkspace}>
            {creatingWorkspace ? "创建中" : "新建"}
          </button>
        </div>
      </div>

      <div className="workspace-actions">
        <button
          className="delete-button topbar-delete-button"
          type="button"
          onClick={onRequestDeleteWorkspace}
          disabled={!activeWorkspaceId || deletingWorkspace}
        >
          删除目录
        </button>
        <button
          className="theme-toggle"
          type="button"
          role="switch"
          aria-checked={theme === "dark"}
          aria-label="切换亮色和暗色模式"
          onClick={onThemeToggle}
        >
          <span className="theme-toggle-track" aria-hidden="true">
            <span className="theme-toggle-thumb" />
          </span>
          <span>{theme === "dark" ? "暗色" : "亮色"}</span>
        </button>
      </div>
    </header>
  );
}
