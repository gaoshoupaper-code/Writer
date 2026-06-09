import { useEffect, useRef, useState } from "react";
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
  onWorkspaceChange: (workspaceId: string) => void;
  onCreateWorkspace: () => void;
  onDeleteWorkspace: (workspaceId: string) => void;
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
  onDeleteWorkspace,
  onThemeToggle,
}: TopBarProps) {
  const activeWorkspace = workspaces.find((w) => w.workspace_id === activeWorkspaceId);
  const displayLabel = activeWorkspace?.outline_name || "选择一个工作目录";

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
                    <span className="truncate text-sm">{workspace.outline_name}</span>
                    <button
                      className="session-option-delete"
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onDeleteWorkspace(workspace.workspace_id);
                      }}
                      disabled={deletingWorkspace || creatingWorkspace}
                      aria-label={`删除 ${workspace.outline_name}`}
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
