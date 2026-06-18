"""风格存储（多用户改造版，D7：完全私有）。

原实现：全局 styles.json + 通过 thread_store 写 workspaces.json 的 active_style_id。
现实现：SQLite styles 表（带 owner_id）+ WorkspaceRepository.set_active_style。

每个用户只看自己的风格，互不可见、互不影响。

注：StyleSummary 来自 app.create_type.schemas（4 列风格字段版），不是 app.schemas.style。
"""

from __future__ import annotations

from app.create_type.schemas import StyleSummary
from app.db import Database, StyleRepository, WorkspaceRepository


class CreateTypeStore:
    def __init__(self, db: Database, workspace_repo: WorkspaceRepository) -> None:
        self.db = db
        self.styles = StyleRepository(db)
        self.workspaces = workspace_repo

    def list_styles(self, owner_id: str) -> list[StyleSummary]:
        rows = self.styles.list_by_owner(owner_id)
        return [self._to_summary(r) for r in rows]

    def get_style(self, owner_id: str, style_id: str) -> dict | None:
        return self.styles.get(style_id, owner_id)

    def create_style(
        self, owner_id: str, name: str, meta_style: str = "",
        storybuilding_style: str = "", detail_outline_style: str = "",
        writing_style: str = "",
    ) -> StyleSummary:
        row = self.styles.create(
            owner_id=owner_id, name=name, meta_style=meta_style,
            storybuilding_style=storybuilding_style,
            detail_outline_style=detail_outline_style,
            writing_style=writing_style,
        )
        return self._to_summary(row)

    def update_style(self, owner_id: str, style_id: str, **fields) -> StyleSummary | None:
        row = self.styles.update(style_id, owner_id, **fields)
        return self._to_summary(row) if row else None

    def delete_style(self, owner_id: str, style_id: str) -> bool:
        ok = self.styles.delete(style_id, owner_id)
        if ok:
            # 清理所有引用此风格的工作区 active_style_id
            self.workspaces.clear_style_reference(style_id)
        return ok

    def get_active_style_id(self, owner_id: str, workspace_id: str) -> str | None:
        ws = self.workspaces.get(workspace_id, owner_id)
        return ws.get("active_style_id") if ws else None

    def set_active_style_id(
        self, owner_id: str, workspace_id: str, style_id: str | None,
    ) -> bool:
        return self.workspaces.set_active_style(workspace_id, owner_id, style_id)

    @staticmethod
    def _to_summary(row: dict) -> StyleSummary:
        return StyleSummary(
            style_id=row["style_id"],
            name=row["name"],
            meta_style=row.get("meta_style", ""),
            storybuilding_style=row.get("storybuilding_style", ""),
            detail_outline_style=row.get("detail_outline_style", ""),
            writing_style=row.get("writing_style", ""),
            created_at=row.get("created_at", ""),
        )
