from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.schemas.style import StyleSummary


class StyleStore:
    def __init__(self, workspace_root: Path, thread_store: ThreadStore) -> None:
        self.workspace_root = workspace_root
        self.thread_store = thread_store
        self.styles_path = workspace_root / "styles.json"
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def list_styles(self) -> list[StyleSummary]:
        index = self._read_index()
        return [StyleSummary(**style) for style in index.values()]

    def get_style(self, style_id: str) -> dict | None:
        return self._read_index().get(style_id)

    def create_style(self, name: str, description: str) -> StyleSummary:
        now = self._now()
        style_id = f"style-{uuid4().hex[:8]}"
        style = {
            "style_id": style_id,
            "name": name.strip(),
            "description": description.strip(),
            "created_at": now,
        }

        index = self._read_index()
        index[style_id] = style
        self._write_index(index)
        return StyleSummary(**style)

    def delete_style(self, style_id: str) -> bool:
        index = self._read_index()
        if style_id not in index:
            return False

        del index[style_id]
        self._write_index(index)

        # Clear any workspace that references the deleted style
        self.thread_store.clear_workspace_style_reference(style_id)
        return True

    def get_active_style_id(self, workspace_id: str) -> str | None:
        workspace = self.thread_store._read_workspace_index().get(workspace_id)
        if workspace is None:
            return None
        return workspace.get("active_style_id")

    def set_active_style_id(self, workspace_id: str, style_id: str | None) -> None:
        workspaces = self.thread_store._read_workspace_index()
        workspace = workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(f"Workspace not found: {workspace_id}")

        if style_id is not None:
            styles = self._read_index()
            if style_id not in styles:
                raise KeyError(f"Style not found: {style_id}")

        workspace["active_style_id"] = style_id
        workspaces[workspace_id] = workspace
        self.thread_store._write_workspace_index(workspaces)

    def _read_index(self) -> dict[str, dict]:
        if not self.styles_path.exists():
            return {}
        with self.styles_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Invalid styles index format: {self.styles_path}")
        return data

    def _write_index(self, index: dict[str, dict]) -> None:
        with self.styles_path.open("w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()


from app.core.thread_store import ThreadStore  # noqa: E402
