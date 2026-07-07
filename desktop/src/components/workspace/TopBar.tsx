import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { WorkspaceSummary } from "../../lib/types";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";

type ThemeMode = "light" | "dark";

type TopBarProps = {
  workspaces: WorkspaceSummary[];
  activeWorkspaceId: string;
  creatingWorkspace: boolean;
  deletingWorkspace: boolean;
  theme: ThemeMode;
  username: string;
  isAdmin: boolean;
  hasApiKey: boolean;
  onWorkspaceChange: (workspaceId: string) => void;
  onCreateWorkspace: () => void;
  onDeleteWorkspace: (workspaceId: string) => void;
  onThemeToggle: () => void;
  onLogout: () => void;
};

export function TopBar({
  workspaces,
  activeWorkspaceId,
  creatingWorkspace,
  deletingWorkspace,
  theme,
  username,
  isAdmin,
  hasApiKey,
  onWorkspaceChange,
  onCreateWorkspace,
  onDeleteWorkspace,
  onThemeToggle,
  onLogout,
}: TopBarProps) {
  const activeWorkspace = workspaces.find((w) => w.workspace_id === activeWorkspaceId);
  // 决策3：顶部「书名：」+ title 静态标签（数据源=workspace.title）
  const displayLabel = activeWorkspace?.title ? `书名：${activeWorkspace.title}` : "选择一个工作目录";

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
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="workspace-menu-trigger" type="button">
                <span>{displayLabel}</span>
                <span className="session-menu-caret">▾</span>
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="center" className="w-[340px]">
              {workspaces.length ? (
                workspaces.map((workspace) => (
                  <DropdownMenuItem
                    key={workspace.workspace_id}
                    className={`flex items-center justify-between gap-2 ${workspace.workspace_id === activeWorkspaceId ? "bg-primary/5" : ""}`}
                    onClick={() => onWorkspaceChange(workspace.workspace_id)}
                  >
                    <span className="truncate text-sm">
                      {workspace.domain === "image" ? "🎨 " : "✍️ "}{workspace.title}
                    </span>
                    <button
                      className="session-option-delete"
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onDeleteWorkspace(workspace.workspace_id);
                      }}
                      disabled={deletingWorkspace || creatingWorkspace}
                      aria-label={`删除 ${workspace.title}`}
                    >
                      删除
                    </button>
                  </DropdownMenuItem>
                ))
              ) : (
                <p className="session-empty">还没有工作目录。</p>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
          <Button
            className="thread-button workspace-create-button bg-gradient-to-br from-[var(--teal)] to-[var(--blue)] shadow-md hover:shadow-lg hover:-translate-y-px transition-all"
            type="button"
            onClick={onCreateWorkspace}
            disabled={creatingWorkspace}
          >
            {creatingWorkspace ? "创建中" : "新建"}
          </Button>
        </div>
      </div>

      <div className="workspace-actions">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="user-menu-trigger" type="button" aria-label="用户菜单">
              <span className="user-menu-avatar">{(username || "?").slice(0, 1).toUpperCase()}</span>
              <span className="user-menu-name">{username || "用户"}</span>
              <span className="session-menu-caret">▾</span>
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-[220px]">
            <div className="user-menu-header">
              <strong>{username || "用户"}</strong>
              <span className={`user-menu-keybadge ${hasApiKey ? "ok" : "missing"}`}>
                {hasApiKey ? "Key 已配置" : "Key 未配置"}
              </span>
            </div>
            <DropdownMenuSeparator />
            <DropdownMenuItem asChild>
              <Link to="/settings">设置（API Key）</Link>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem className="text-red-600" onClick={onLogout}>
              退出登录
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

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
