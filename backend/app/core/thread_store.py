"""工作区与线程元数据存储（多用户改造版，PR-09 拆分后）。

职责单一化：只管元数据 CRUD（SQLite）+ owner 限定路径解析。
产物文件读写（outline/storyline/detail/novel/character/worldview）已迁到
``WritingArtifactStore``（thread_store.artifacts）。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.db import (
    Database,
    ThreadRepository,
    WorkspaceRepository,
    workspace_dir,
)
from app.schemas.screenplay import (
    ThreadSummary,
    WorkspaceSummary,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ThreadStore:
    """工作区/线程存储门面。元数据走 SQLite，文件走 owner 限定目录。"""

    def __init__(self, db: Database, workspace_root: Path) -> None:
        self.db = db
        self.workspace_root = workspace_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.workspaces = WorkspaceRepository(db)
        self.threads = ThreadRepository(db)
        # 产物存储：共享 workspace_root + threads repo（write 需要 touch 时间戳）
        from app.core.artifact_store import WritingArtifactStore
        self.artifacts = WritingArtifactStore(workspace_root, self.threads)

    # ── 路径解析（owner 维度）────────────────────────────────
    def _ws_path(self, owner_id: str, workspace_id: str) -> Path:
        return workspace_dir(self.workspace_root, owner_id, workspace_id)

    def _ws_row(self, owner_id: str, workspace_id: str, *, admin_override: bool = False) -> dict | None:
        return self.workspaces.get_any(workspace_id) if admin_override \
            else self.workspaces.get(workspace_id, owner_id)

    # ── 工作区 CRUD ──────────────────────────────────────────
    def list_workspaces(self, owner_id: str) -> list[WorkspaceSummary]:
        rows = self.workspaces.list_by_owner(owner_id)
        return [self._to_workspace_summary(r) for r in rows]

    def create_workspace(self, owner_id: str, title: str, domain: str = "writing") -> WorkspaceSummary:
        normalized = title.strip()
        if not normalized:
            raise ValueError("title cannot be empty")
        ws = self.workspaces.create(owner_id=owner_id, title=normalized, domain=domain)
        ws_path = self._ws_path(owner_id, ws["workspace_id"])
        ws_path.mkdir(parents=True, exist_ok=False)
        return self._to_workspace_summary({**ws, "workspace_path": str(ws_path)})

    def get_workspace(self, owner_id: str, workspace_id: str) -> WorkspaceSummary | None:
        ws = self.workspaces.get(workspace_id, owner_id)
        if ws is None:
            return None
        return self._to_workspace_summary({**ws, "workspace_path": str(self._ws_path(owner_id, workspace_id))})

    def delete_workspace(self, owner_id: str, workspace_id: str) -> list[str] | None:
        ws = self.workspaces.get(workspace_id, owner_id)
        if ws is None:
            return None
        ws_path = self._ws_path(owner_id, workspace_id)
        # 收集被删 thread_id（供 main.py 清理 checkpoint）
        thread_rows = self.threads.list_by_workspace(workspace_id, owner_id)
        deleted_thread_ids = [t["thread_id"] for t in thread_rows]
        if ws_path.exists():
            shutil.rmtree(ws_path, ignore_errors=True)
        self.workspaces.delete(workspace_id, owner_id)
        return deleted_thread_ids

    # ── 线程 CRUD ────────────────────────────────────────────
    def list_threads(self, owner_id: str, workspace_id: str | None = None) -> list[ThreadSummary]:
        if workspace_id is not None:
            rows = self.threads.list_by_workspace(workspace_id, owner_id)
        else:
            rows = self.threads.list_by_owner(owner_id)
        ws_cache: dict[str, Path] = {}
        return [self._to_thread_summary(r, owner_id, ws_cache) for r in rows]

    def get_thread(self, owner_id: str, thread_id: str) -> ThreadSummary | None:
        row = self.threads.get(thread_id, owner_id)
        if row is None:
            return None
        return self._to_thread_summary(row, owner_id)

    def create_thread(
        self, owner_id: str, workspace_id: str, session_name: str | None = None,
    ) -> ThreadSummary:
        ws = self.workspaces.get(workspace_id, owner_id)
        if ws is None:
            raise KeyError(f"Workspace not found: {workspace_id}")
        thread = self.threads.create(
            workspace_id=workspace_id, owner_id=owner_id, session_name=session_name,
        )
        return self._to_thread_summary(thread, owner_id)

    def delete_thread(self, owner_id: str, thread_id: str) -> bool:
        return self.threads.delete(thread_id, owner_id)

    def update_thread_name(self, owner_id: str, thread_id: str, session_name: str) -> ThreadSummary | None:
        try:
            row = self.threads.update_name(thread_id, owner_id, session_name)
        except ValueError:
            raise
        if row is None:
            return None
        return self._to_thread_summary(row, owner_id)

    # ── bootstrap（元数据 + 产物聚合，产物部分委托 artifacts）──
        """一次返回 workspace 元数据 + 全部产物。

        元数据（workspace + threads）由本类查询，产物读取委托给 artifacts。
        """
        ws = self.workspaces.get(workspace_id, owner_id)
        if ws is None:
            return None
        ws_path = self._ws_path(owner_id, workspace_id)
        if not ws_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {ws_path}")

        thread_rows = self.threads.list_by_workspace(workspace_id, owner_id)
        thread_summaries = [self._to_thread_summary(t, owner_id) for t in thread_rows]

        artifact_data = self.artifacts.bootstrap_workspace(
            owner_id, workspace_id,
            ws_exists=True, threads_rows=thread_rows, thread_summaries=thread_summaries,
        )
        return {"workspace": ws, **artifact_data}

    def clear_workspace_style_reference(self, style_id: str) -> None:
        self.workspaces.clear_style_reference(style_id)

    # ── 辅助 ────────────────────────────────────────────────
    def _to_workspace_summary(self, ws: dict) -> WorkspaceSummary:
        return WorkspaceSummary(
            workspace_id=ws["workspace_id"],
            title=ws.get("title", ws.get("outline_name", "")),
            domain=ws.get("domain", "writing"),
            workspace_path=ws.get("workspace_path", ""),
            created_at=ws["created_at"],
            updated_at=ws["updated_at"],
            session_count=ws.get("session_count", 0),
            active_style_id=ws.get("active_style_id"),
        )

    def _to_thread_summary(
        self, thread: dict, owner_id: str, ws_cache: dict[str, Path] | None = None,
    ) -> ThreadSummary:
        ws_id = thread["workspace_id"]
        if ws_cache is not None and ws_id in ws_cache:
            ws_path = ws_cache[ws_id]
        else:
            ws_path = self._ws_path(owner_id, ws_id)
            if ws_cache is not None:
                ws_cache[ws_id] = ws_path
        return ThreadSummary(
            thread_id=thread["thread_id"],
            workspace_id=ws_id,
            session_name=thread["session_name"],
            workspace_path=str(ws_path),
            created_at=thread["created_at"],
            updated_at=thread["updated_at"],
        )
