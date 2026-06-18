"""工作区与线程存储（多用户改造版）。

原实现：全局 JSON 索引（workspaces.json/threads.json）+ workspace/<作品名>/。
现实现：SQLite 元数据（WorkspaceRepository/ThreadRepository）+ workspace/<owner_id>/<workspace_id>/。

职责：
- 元数据 CRUD（委派给 Repository，带 owner_id）
- owner 限定的工作区路径解析
- 工作区文件读写（outline/storyline/detail/character/novel/worldview）

文件读写逻辑保持原样；唯一变化是路径来源从 JSON 索引改为 owner 限定的目录。
"""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.db import (
    Database,
    ThreadRepository,
    WorkspaceRepository,
    workspace_dir,
)
from app.schemas.character import CharacterGenerateResponse
from app.schemas.screenplay import (
    CharacterMarkdownFile,
    DetailOutlineChapter,
    ScreenplayGenerateResponse,
    StorylineEntry,
    ThreadSummary,
    WorkspaceCharacterContent,
    WorkspaceDetailOutlineContent,
    WorkspaceNovelChapter,
    WorkspaceNovelChaptersContent,
    WorkspaceNovelContent,
    WorkspaceOutlineContent,
    StorylineGraphEvent,
    StorylineGraphStoryline,
    WorkspaceStorylineContent,
    WorkspaceStorylineGraphContent,
    WorkspaceWorldviewContent,
    WorkspaceSummary,
)
from app.writer.expert_agent.services.storyline_graph import (
    build_storyline_graph_data,
    generate_storyline_graph,
    is_stale,
)


def _read_text(path: Path) -> str:
    """读取文件文本，UTF-8 失败时回退 GB18030（GBK 超集，兼容中文 Windows）。"""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gb18030", errors="replace")


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

    # ── 工作区文件读取 ───────────────────────────────────────
    def _require_ws_path(self, owner_id: str, workspace_id: str) -> Path:
        ws = self.workspaces.get(workspace_id, owner_id)
        if ws is None:
            raise KeyError(workspace_id)
        ws_path = self._ws_path(owner_id, workspace_id)
        if not ws_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {ws_path}")
        return ws_path

    def read_workspace_outline(self, owner_id: str, workspace_id: str) -> WorkspaceOutlineContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except KeyError:
            return None
        artifact = ws_path / "outline.md"
        markdown = _read_text(artifact) if artifact.exists() else ""
        return WorkspaceOutlineContent(workspace_id=workspace_id, markdown=markdown)

    def read_workspace_storyline(self, owner_id: str, workspace_id: str) -> WorkspaceStorylineContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except KeyError:
            return None
        index_path = ws_path / "storyline.md"
        index_markdown = _read_text(index_path) if index_path.exists() else ""
        entries: list[StorylineEntry] = []
        storyline_dir = ws_path / "storyline"
        if storyline_dir.exists():
            for ap in sorted(storyline_dir.glob("*.md"), key=lambda p: p.name):
                content = _read_text(ap).strip()
                if content:
                    entries.append(StorylineEntry(filename=ap.name, title=ap.stem, markdown=content))
        return WorkspaceStorylineContent(
            workspace_id=workspace_id, index_markdown=index_markdown,
            entries=entries, file_count=len(entries),
        )

    def read_workspace_storyline_graph(self, owner_id: str, workspace_id: str) -> WorkspaceStorylineGraphContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except KeyError:
            return None
        if is_stale(ws_path):
            generate_storyline_graph(ws_path)
        data = build_storyline_graph_data(ws_path)
        graph_path = ws_path / "storyline_graph.md"
        return WorkspaceStorylineGraphContent(
            workspace_id=workspace_id,
            events=[StorylineGraphEvent(**e) for e in data.get("events", [])],
            storylines=[StorylineGraphStoryline(**s) for s in data.get("storylines", [])],
            markdown=_read_text(graph_path) if graph_path.exists() else "",
        )

    def read_workspace_worldview(self, owner_id: str, workspace_id: str) -> WorkspaceWorldviewContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except KeyError:
            return None
        wp = ws_path / "worldview.md"
        return WorkspaceWorldviewContent(
            workspace_id=workspace_id,
            markdown=_read_text(wp) if wp.exists() else "",
        )

    def read_workspace_detail_outline(self, owner_id: str, workspace_id: str) -> WorkspaceDetailOutlineContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except KeyError:
            return None
        detail_dir = ws_path / "detail"
        chapters: list[DetailOutlineChapter] = []
        if detail_dir.exists():
            for ap in sorted(detail_dir.glob("*.md"), key=lambda p: (p.name != "overview.md", p.name)):
                if ap.name == "evaluation.md":
                    continue
                content = _read_text(ap).strip()
                if content:
                    chapters.append(DetailOutlineChapter(
                        filename=ap.name, title=self._detail_outline_title(ap.name), markdown=content,
                    ))
        return WorkspaceDetailOutlineContent(workspace_id=workspace_id, chapters=chapters, file_count=len(chapters))

    def read_thread_outline(self, owner_id: str, thread_id: str) -> WorkspaceOutlineContent | None:
        thread = self.threads.get(thread_id, owner_id)
        if thread is None:
            return None
        return self.read_workspace_outline(owner_id, thread["workspace_id"])

    def read_workspace_novel(self, owner_id: str, workspace_id: str) -> WorkspaceNovelContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except KeyError:
            return None
        novel_path = ws_path / "novel.md"
        markdown = _read_text(novel_path) if novel_path.exists() else ""
        return WorkspaceNovelContent(workspace_id=workspace_id, markdown=markdown)

    def read_workspace_novel_chapters(self, owner_id: str, workspace_id: str) -> WorkspaceNovelChaptersContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except KeyError:
            return None
        chapter_files = self._workspace_chapter_files(ws_path)
        chapters: list[WorkspaceNovelChapter] = []
        for path in chapter_files:
            md = _read_text(path)
            chapters.append(WorkspaceNovelChapter(
                filename=path.name, title=self._markdown_title(md) or path.stem, markdown=md,
            ))
        novel_md_path = ws_path / "novel.md"
        if novel_md_path.exists():
            md = _read_text(novel_md_path)
            chapters = [WorkspaceNovelChapter(
                filename="novel.md", title=self._markdown_title(md) or "正文", markdown=md,
            )] + chapters
        return WorkspaceNovelChaptersContent(workspace_id=workspace_id, chapters=chapters, file_count=len(chapters))

    def read_workspace_characters(self, owner_id: str, workspace_id: str) -> WorkspaceCharacterContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except KeyError:
            return None
        character_dir = ws_path / "character"
        characters: list[CharacterMarkdownFile] = []
        if character_dir.exists():
            for ap in sorted(character_dir.glob("*.md"), key=lambda p: p.stem):
                characters.append(CharacterMarkdownFile(filename=ap.name, name=ap.stem, markdown=_read_text(ap)))
        return WorkspaceCharacterContent(workspace_id=workspace_id, characters=characters)

    def bootstrap_workspace(self, owner_id: str, workspace_id: str) -> dict | None:
        """一次读取 index 文件，批量返回 bootstrap 所需的全部数据。"""
        ws = self.workspaces.get(workspace_id, owner_id)
        if ws is None:
            return None
        ws_path = self._ws_path(owner_id, workspace_id)
        if not ws_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {ws_path}")

        thread_summaries = [
            self._to_thread_summary(t, owner_id)
            for t in self.threads.list_by_workspace(workspace_id, owner_id)
        ]

        artifact_path = ws_path / "outline.md"
        outline = WorkspaceOutlineContent(
            workspace_id=workspace_id,
            markdown=_read_text(artifact_path) if artifact_path.exists() else "",
        )

        storyline_index_path = ws_path / "storyline.md"
        storyline_entries: list[StorylineEntry] = []
        storyline_dir = ws_path / "storyline"
        if storyline_dir.exists():
            for ap in sorted(storyline_dir.glob("*.md"), key=lambda p: p.name):
                content = _read_text(ap).strip()
                if content:
                    storyline_entries.append(StorylineEntry(filename=ap.name, title=ap.stem, markdown=content))
        storyline = WorkspaceStorylineContent(
            workspace_id=workspace_id,
            index_markdown=_read_text(storyline_index_path) if storyline_index_path.exists() else "",
            entries=storyline_entries, file_count=len(storyline_entries),
        )

        worldview_path = ws_path / "worldview.md"
        worldview = WorkspaceWorldviewContent(
            workspace_id=workspace_id,
            markdown=_read_text(worldview_path) if worldview_path.exists() else "",
        )

        detail_dir = ws_path / "detail"
        detail_chapters: list[DetailOutlineChapter] = []
        if detail_dir.exists():
            for ap in sorted(detail_dir.glob("*.md"), key=lambda p: (p.name != "overview.md", p.name)):
                if ap.name == "evaluation.md":
                    continue
                content = _read_text(ap).strip()
                if content:
                    detail_chapters.append(DetailOutlineChapter(
                        filename=ap.name, title=self._detail_outline_title(ap.name), markdown=content,
                    ))
        detail_outline = WorkspaceDetailOutlineContent(
            workspace_id=workspace_id, chapters=detail_chapters, file_count=len(detail_chapters),
        )

        character_dir = ws_path / "character"
        characters: list[CharacterMarkdownFile] = []
        if character_dir.exists():
            for ap in sorted(character_dir.glob("*.md"), key=lambda p: p.stem):
                characters.append(CharacterMarkdownFile(filename=ap.name, name=ap.stem, markdown=_read_text(ap)))
        character_content = WorkspaceCharacterContent(workspace_id=workspace_id, characters=characters)

        novel = self.read_workspace_novel_chapters(owner_id, workspace_id)

        return {
            "workspace": ws,
            "threads": sorted(thread_summaries, key=lambda t: t.updated_at, reverse=True),
            "outline": outline,
            "storyline": storyline,
            "detail_outline": detail_outline,
            "characters": character_content,
            "worldview": worldview,
            "novel": novel,
        }

    # ── 工作区文件写入 ───────────────────────────────────────
    def write_outline(
        self, owner_id: str, thread: ThreadSummary, response: ScreenplayGenerateResponse,
    ) -> None:
        ws_path = Path(thread.workspace_path)
        if not ws_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {ws_path}")
        artifact_path = ws_path / "outline.md"
        markdown = response.markdown.strip() or self._fallback_outline_markdown(response)
        artifact_path.write_text(f"{markdown}\n", encoding="utf-8")
        evaluation_markdown = response.evaluation_markdown.strip()
        if evaluation_markdown:
            (ws_path / "evaluation.md").write_text(f"{evaluation_markdown}\n", encoding="utf-8")
        self.threads.touch(thread.thread_id, owner_id)

    def write_character(
        self, owner_id: str, thread: ThreadSummary, response: CharacterGenerateResponse,
    ) -> None:
        ws_path = Path(thread.workspace_path)
        if not ws_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {ws_path}")
        artifact_dir = ws_path / "character"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / f"{response.name}.md"
        markdown = response.markdown.strip() or self._fallback_character_markdown(response)
        artifact_path.write_text(f"{markdown}\n", encoding="utf-8")
        self.threads.touch(thread.thread_id, owner_id)

    def clear_workspace_style_reference(self, style_id: str) -> None:
        self.workspaces.clear_style_reference(style_id)

    # ── 辅助 ────────────────────────────────────────────────
    def _detail_outline_title(self, filename: str) -> str:
        if filename == "overview.md":
            return "总览"
        match = re.match(r"chapter-(\d+)\.md$", filename)
        if match:
            return f"第{int(match.group(1))}章"
        return Path(filename).stem

    def _workspace_chapter_files(self, ws_path: Path) -> list[Path]:
        chapter_dir = ws_path / "chapter"
        if not chapter_dir.exists():
            return []
        return sorted(
            (p for p in chapter_dir.glob("*.md") if p.is_file() and _read_text(p).strip()),
            key=lambda p: p.name,
        )

    def _markdown_title(self, markdown: str) -> str:
        for line in markdown.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
            if stripped:
                return stripped
        return ""

    def _fallback_character_markdown(self, response: CharacterGenerateResponse) -> str:
        return (
            f"# {response.name}\n\n"
            f"## 角色身份\n\n{response.identity}\n\n"
            f"## 外貌特征\n\n{response.appearance}\n\n"
            f"## 性格与内心\n\n{response.personality}\n\n"
            f"## 关系网络\n\n{response.relationships}\n\n"
            f"## 目前状态\n\n{response.current_state}\n"
        )

    def _fallback_outline_markdown(self, response: ScreenplayGenerateResponse) -> str:
        beat_lines = "\n".join(
            f"{i}. {beat}" for i, beat in enumerate(response.beats, start=1)
        )
        return (
            f"# {response.title}\n\n"
            f"## 一句话梗概\n\n{response.logline}\n\n"
            f"## 短梗概\n\n{response.synopsis}\n\n"
            f"## 五个关键剧情节点\n\n{beat_lines}"
        )

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
