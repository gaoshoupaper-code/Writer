"""写作产物存储（PR-09 从 ThreadStore 拆出）。

职责单一化：ThreadStore 只管元数据 CRUD，产物文件读写（outline/storyline/
detail/novel/character/worldview）归本类。

注入 threads repository（write_outline/write_character 需要 touch 时间戳）。
路径解析仍用 owner 限定的 workspace 目录（与 ThreadStore 共享 workspace_root）。

PR-11 writer 降级时随迁 domains/writing/。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.db import ThreadRepository, workspace_dir
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
    WorkspaceStorylineContent,
    WorkspaceStorylineGraphContent,
    WorkspaceWorldviewContent,
)


def _read_text(path: Path) -> str:
    """读取文件文本，UTF-8 失败时回退 GB18030（GBK 超集，兼容中文 Windows）。"""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gb18030", errors="replace")


class WritingArtifactStore:
    """写作产物文件读写（owner 限定的 workspace 目录）。

    由 ThreadStore 持有（thread_store.artifacts），或独立注入到 main.py。
    """

    def __init__(
        self,
        workspace_root: Path,
        threads: ThreadRepository,
    ) -> None:
        self.workspace_root = workspace_root
        self.threads = threads

    # ── 路径解析 ─────────────────────────────────────────────
    def _ws_path(self, owner_id: str, workspace_id: str) -> Path:
        return workspace_dir(self.workspace_root, owner_id, workspace_id)

    def _require_ws_path(self, owner_id: str, workspace_id: str) -> Path:
        """owner 限定路径，目录缺失抛 FileNotFoundError，workspace 不存在抛 KeyError。"""
        ws_path = self._ws_path(owner_id, workspace_id)
        if not ws_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {ws_path}")
        return ws_path

    # ── 产物读取 ─────────────────────────────────────────────
    def read_workspace_outline(self, owner_id: str, workspace_id: str) -> WorkspaceOutlineContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except (KeyError, FileNotFoundError):
            return None
        artifact = ws_path / "outline.md"
        markdown = _read_text(artifact) if artifact.exists() else ""
        return WorkspaceOutlineContent(workspace_id=workspace_id, markdown=markdown)

    def read_workspace_storyline(self, owner_id: str, workspace_id: str) -> WorkspaceStorylineContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except (KeyError, FileNotFoundError):
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
        """读取 storyline_graph.md（派生产物，只读不生成）。

        「按需生成」兜底逻辑在 API 层（main.py:get_workspace_storyline_graph）。
        本方法只返回 markdown 文本，结构化字段由 API 层填充。
        """
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except (KeyError, FileNotFoundError):
            return None
        graph_path = ws_path / "storyline_graph.md"
        return WorkspaceStorylineGraphContent(
            workspace_id=workspace_id,
            markdown=_read_text(graph_path) if graph_path.exists() else "",
        )

    def read_workspace_worldview(self, owner_id: str, workspace_id: str) -> WorkspaceWorldviewContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except (KeyError, FileNotFoundError):
            return None
        wp = ws_path / "worldview.md"
        return WorkspaceWorldviewContent(
            workspace_id=workspace_id,
            markdown=_read_text(wp) if wp.exists() else "",
        )

    def read_workspace_detail_outline(self, owner_id: str, workspace_id: str) -> WorkspaceDetailOutlineContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except (KeyError, FileNotFoundError):
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
        except (KeyError, FileNotFoundError):
            return None
        novel_path = ws_path / "novel.md"
        markdown = _read_text(novel_path) if novel_path.exists() else ""
        return WorkspaceNovelContent(workspace_id=workspace_id, markdown=markdown)

    def read_workspace_novel_chapters(self, owner_id: str, workspace_id: str) -> WorkspaceNovelChaptersContent | None:
        try:
            ws_path = self._require_ws_path(owner_id, workspace_id)
        except (KeyError, FileNotFoundError):
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
        except (KeyError, FileNotFoundError):
            return None
        character_dir = ws_path / "character"
        characters: list[CharacterMarkdownFile] = []
        if character_dir.exists():
            for ap in sorted(character_dir.glob("*.md"), key=lambda p: p.stem):
                characters.append(CharacterMarkdownFile(filename=ap.name, name=ap.stem, markdown=_read_text(ap)))
        return WorkspaceCharacterContent(workspace_id=workspace_id, characters=characters)

    def bootstrap_workspace(
        self, owner_id: str, workspace_id: str, *, ws_exists: bool, threads_rows: list[dict],
        thread_summaries: list[ThreadSummary],
    ) -> dict | None:
        """一次读取全部产物文件，批量返回 bootstrap 数据。

        Args:
            ws_exists: workspace 目录是否存在（由调用方 ThreadStore 判定，避免跨界）。
            threads_rows: workspace 的 thread 原始行（由 ThreadStore 查询）。
            thread_summaries: 已转换的 ThreadSummary 列表（由 ThreadStore 转换）。
        """
        ws_path = self._ws_path(owner_id, workspace_id)
        if not ws_exists and not ws_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {ws_path}")

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
            "threads": sorted(thread_summaries, key=lambda t: t.updated_at, reverse=True),
            "outline": outline,
            "storyline": storyline,
            "detail_outline": detail_outline,
            "characters": character_content,
            "worldview": worldview,
            "novel": novel,
        }

    # ── 产物写入 ─────────────────────────────────────────────
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
