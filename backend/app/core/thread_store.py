from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.schemas.character import CharacterGenerateResponse


def _read_text(path: Path) -> str:
    """读取文件文本，UTF-8 失败时回退 GB18030（GBK 超集，兼容中文 Windows）。"""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gb18030", errors="replace")


from app.schemas.screenplay import (
    CharacterMarkdownFile,
    DetailOutlineChapter,
    ScreenplayGenerateResponse,
    StorylineEntry,
    ThreadSummary,
    VolumeChapter,
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
    WorkspaceVolumeContent,
    WorkspaceWorldviewContent,
    WorkspaceSummary,
)
from app.writer.expert_agent.services.storyline_graph import (
    build_storyline_graph_data,
    generate_storyline_graph,
    is_stale,
)


class ThreadStore:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root
        self.workspace_index_path = workspace_root / "workspaces.json"
        self.thread_index_path = workspace_root / "threads.json"
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_indexes()

    def list_workspaces(self) -> list[WorkspaceSummary]:
        workspaces = self._read_workspace_index()
        threads = self._read_thread_index()
        summaries = []
        for workspace in workspaces.values():
            workspace_id = str(workspace["workspace_id"])
            workspace["session_count"] = sum(
                1 for thread in threads.values() if thread["workspace_id"] == workspace_id
            )
            summaries.append(self._to_workspace_summary(workspace))
        return sorted(summaries, key=lambda workspace: workspace.updated_at, reverse=True)

    def create_workspace(self, outline_name: str) -> WorkspaceSummary:
        normalized_name = outline_name.strip()
        if not normalized_name:
            raise ValueError("outline_name cannot be empty")

        now = self._now()
        workspace_id = self._sanitize_workspace_name(normalized_name)
        workspace_path = self._workspace_dir(workspace_id)
        if workspace_path.exists():
            raise FileExistsError(f"Workspace already exists: {workspace_id}")

        workspace_path.mkdir(parents=True, exist_ok=False)
        workspace = {
            "workspace_id": workspace_id,
            "outline_name": normalized_name,
            "workspace_path": str(workspace_path),
            "created_at": now,
            "updated_at": now,
            "session_count": 0,
        }

        workspaces = self._read_workspace_index()
        workspaces[workspace_id] = workspace
        self._write_workspace_index(workspaces)
        return self._to_workspace_summary(workspace)

    def get_workspace(self, workspace_id: str) -> WorkspaceSummary | None:
        workspace = self._read_workspace_index().get(workspace_id)
        if workspace is None:
            return None
        workspace["session_count"] = sum(
            1 for thread in self._read_thread_index().values() if thread["workspace_id"] == workspace_id
        )
        return self._to_workspace_summary(workspace)

    def list_threads(self, workspace_id: str | None = None) -> list[ThreadSummary]:
        threads = self._read_thread_index().values()
        if workspace_id is not None:
            threads = [thread for thread in threads if thread["workspace_id"] == workspace_id]
        summaries = [self._to_thread_summary(thread) for thread in threads]
        return sorted(summaries, key=lambda thread: thread.updated_at, reverse=True)

    def get_thread(self, thread_id: str) -> ThreadSummary | None:
        thread = self._read_thread_index().get(thread_id)
        if thread is None:
            return None
        return self._to_thread_summary(thread)

    def create_thread(
        self,
        workspace_id: str,
        session_name: str | None = None,
    ) -> ThreadSummary:
        workspace = self._read_workspace_index().get(workspace_id)
        if workspace is None:
            raise KeyError(f"Workspace not found: {workspace_id}")

        existing_threads = [
            thread for thread in self._read_thread_index().values() if thread["workspace_id"] == workspace_id
        ]
        normalized_name = session_name.strip() if session_name is not None else ""
        if normalized_name == "":
            normalized_name = f"会话 {len(existing_threads) + 1}"

        now = self._now()
        thread_id = self._make_thread_id(workspace_id, normalized_name)
        thread = {
            "thread_id": thread_id,
            "workspace_id": workspace_id,
            "session_name": normalized_name,
            "workspace_path": workspace["workspace_path"],
            "created_at": now,
            "updated_at": now,
        }

        threads = self._read_thread_index()
        threads[thread_id] = thread
        self._write_thread_index(threads)
        self._touch_workspace(workspace_id)
        return self._to_thread_summary(thread)

    def delete_thread(self, thread_id: str) -> bool:
        threads = self._read_thread_index()
        thread = threads.get(thread_id)
        if thread is None:
            return False

        del threads[thread_id]
        self._write_thread_index(threads)
        self._touch_workspace(thread["workspace_id"])
        return True

    def update_thread_name(self, thread_id: str, session_name: str) -> ThreadSummary | None:
        normalized_name = session_name.strip()
        if normalized_name == "":
            raise ValueError("Session name cannot be empty")

        threads = self._read_thread_index()
        thread = threads.get(thread_id)
        if thread is None:
            return None

        thread["session_name"] = normalized_name
        thread["updated_at"] = self._now()
        threads[thread_id] = thread
        self._write_thread_index(threads)
        self._touch_workspace(thread["workspace_id"])
        return self._to_thread_summary(thread)

    def delete_workspace(self, workspace_id: str) -> list[str] | None:
        workspaces = self._read_workspace_index()
        workspace = workspaces.get(workspace_id)
        if workspace is None:
            return None

        workspace_path = Path(workspace["workspace_path"])
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        threads = self._read_thread_index()
        deleted_thread_ids = [
            thread_id
            for thread_id, thread in threads.items()
            if thread["workspace_id"] == workspace_id
        ]
        remaining_threads = {
            thread_id: thread
            for thread_id, thread in threads.items()
            if thread["workspace_id"] != workspace_id
        }

        shutil.rmtree(workspace_path)
        del workspaces[workspace_id]
        self._write_workspace_index(workspaces)
        self._write_thread_index(remaining_threads)
        return deleted_thread_ids

    def read_workspace_outline(self, workspace_id: str) -> WorkspaceOutlineContent | None:
        workspace = self._read_workspace_index().get(workspace_id)
        if workspace is None:
            return None

        workspace_path = Path(workspace["workspace_path"])
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        artifact_path = workspace_path / "outline.md"
        markdown = _read_text(artifact_path) if artifact_path.exists() else ""
        return WorkspaceOutlineContent(workspace_id=workspace_id, markdown=markdown)

    def read_workspace_storyline(self, workspace_id: str) -> WorkspaceStorylineContent | None:
        workspace = self._read_workspace_index().get(workspace_id)
        if workspace is None:
            return None

        workspace_path = Path(workspace["workspace_path"])
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        index_path = workspace_path / "storyline.md"
        index_markdown = _read_text(index_path) if index_path.exists() else ""

        entries: list[StorylineEntry] = []
        storyline_dir = workspace_path / "storyline"
        if storyline_dir.exists():
            for artifact_path in sorted(storyline_dir.glob("*.md"), key=lambda p: p.name):
                content = _read_text(artifact_path).strip()
                if content:
                    entries.append(
                        StorylineEntry(
                            filename=artifact_path.name,
                            title=artifact_path.stem,
                            markdown=content,
                        )
                    )

        return WorkspaceStorylineContent(
            workspace_id=workspace_id,
            index_markdown=index_markdown,
            entries=entries,
            file_count=len(entries),
        )

    def read_workspace_storyline_graph(self, workspace_id: str) -> WorkspaceStorylineGraphContent | None:
        """读取故事线流程图（storyline_graph.md），缺失或过期时按需生成（幂等兜底）。

        按需兜底：storyline_graph.md 是派生视图，靠 storybuilding 完成触发；
        但触发链路不可靠（stream 常异常退出），故读取时若 ``is_stale``（图缺失或
        源文件更新）则重生成，保证前端拿到的图始终与 storyline.md 一致。
        """
        workspace = self._read_workspace_index().get(workspace_id)
        if workspace is None:
            return None
        workspace_path = Path(workspace["workspace_path"])
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        stale = is_stale(workspace_path)
        if stale:
            generate_storyline_graph(workspace_path)

        data = build_storyline_graph_data(workspace_path)
        graph_path = workspace_path / "storyline_graph.md"
        generated_at = (
            datetime.fromtimestamp(graph_path.stat().st_mtime, tz=UTC).isoformat()
            if graph_path.exists()
            else ""
        )
        if data is None:
            return WorkspaceStorylineGraphContent(
                workspace_id=workspace_id,
                markdown="",
                generated_at=generated_at,
                stale=stale,
            )
        return WorkspaceStorylineGraphContent(
            workspace_id=workspace_id,
            markdown=data.markdown,
            storylines=[
                StorylineGraphStoryline(
                    id=s.id,
                    name=s.name,
                    type=s.type,
                    status=s.status,
                    direction=s.direction,
                    key_events=list(s.key_events),
                )
                for s in data.storylines
            ],
            events={
                eid: StorylineGraphEvent(
                    id=ev.id,
                    name=ev.name,
                    type=ev.type,
                    storylines=list(ev.storylines),
                    group=ev.group,
                    doc_order=ev.doc_order,
                )
                for eid, ev in data.events.items()
            },
            t_map=dict(data.t_map),
            storyline_count=len(data.storylines),
            event_count=len(data.events),
            generated_at=generated_at,
            stale=stale,
        )

    def read_workspace_worldview(self, workspace_id: str) -> WorkspaceWorldviewContent | None:
        workspace = self._read_workspace_index().get(workspace_id)
        if workspace is None:
            return None

        workspace_path = Path(workspace["workspace_path"])
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        artifact_path = workspace_path / "worldview.md"
        markdown = _read_text(artifact_path) if artifact_path.exists() else ""
        return WorkspaceWorldviewContent(workspace_id=workspace_id, markdown=markdown)

    def _volume_chapter_title(self, filename: str) -> str:
        if filename == "overview.md":
            return "总览"
        match = re.match(r"volume-(\d+)\.md$", filename)
        if match:
            return f"第{int(match.group(1))}卷"
        return Path(filename).stem

    def read_workspace_volume(self, workspace_id: str) -> WorkspaceVolumeContent | None:
        workspace = self._read_workspace_index().get(workspace_id)
        if workspace is None:
            return None

        workspace_path = Path(workspace["workspace_path"])
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        volume_dir = workspace_path / "volume"
        chapters: list[VolumeChapter] = []
        if volume_dir.exists():
            for artifact_path in sorted(volume_dir.glob("*.md"), key=lambda p: p.name):
                content = _read_text(artifact_path).strip()
                if content:
                    chapters.append(
                        VolumeChapter(
                            filename=artifact_path.name,
                            title=self._volume_chapter_title(artifact_path.name),
                            markdown=content,
                        )
                    )

        return WorkspaceVolumeContent(workspace_id=workspace_id, chapters=chapters, file_count=len(chapters))

    def read_workspace_detail_outline(self, workspace_id: str) -> WorkspaceDetailOutlineContent | None:
        workspace = self._read_workspace_index().get(workspace_id)
        if workspace is None:
            return None

        workspace_path = Path(workspace["workspace_path"])
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        detail_dir = workspace_path / "detail"
        chapters: list[DetailOutlineChapter] = []
        if detail_dir.exists():
            for artifact_path in sorted(detail_dir.glob("*.md"), key=lambda p: (p.name != "overview.md", p.name)):
                if artifact_path.name == "evaluation.md":
                    continue
                content = _read_text(artifact_path).strip()
                if content:
                    chapters.append(
                        DetailOutlineChapter(
                            filename=artifact_path.name,
                            title=self._detail_outline_title(artifact_path.name),
                            markdown=content,
                        )
                    )

        return WorkspaceDetailOutlineContent(workspace_id=workspace_id, chapters=chapters, file_count=len(chapters))

    def read_thread_outline(self, thread_id: str) -> WorkspaceOutlineContent | None:
        thread = self.get_thread(thread_id)
        if thread is None:
            return None
        return self.read_workspace_outline(thread.workspace_id)

    def read_workspace_novel(self, workspace_id: str) -> WorkspaceNovelContent | None:
        workspace = self._read_workspace_index().get(workspace_id)
        if workspace is None:
            return None

        workspace_path = Path(workspace["workspace_path"])
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        chapter_files = self._workspace_chapter_files(workspace_path)
        if chapter_files:
            markdown = "\n\n".join(_read_text(path).strip() for path in chapter_files)
            return WorkspaceNovelContent(
                workspace_id=workspace_id,
                markdown=f"{markdown}\n" if markdown else "",
                source="chapter/",
                chapter_count=len(chapter_files),
            )

        artifact_path = workspace_path / "novel.md"
        markdown = _read_text(artifact_path) if artifact_path.exists() else ""
        return WorkspaceNovelContent(
            workspace_id=workspace_id,
            markdown=markdown,
            source="novel.md",
            chapter_count=0,
        )

    def read_workspace_novel_chapters(self, workspace_id: str) -> WorkspaceNovelChaptersContent | None:
        workspace = self._read_workspace_index().get(workspace_id)
        if workspace is None:
            return None

        workspace_path = Path(workspace["workspace_path"])
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        chapter_files = self._workspace_chapter_files(workspace_path)
        if chapter_files:
            chapters = [
                WorkspaceNovelChapter(
                    filename=path.name,
                    title=self._markdown_title(_read_text(path)) or path.stem,
                    markdown=_read_text(path).strip(),
                )
                for path in chapter_files
            ]
            return WorkspaceNovelChaptersContent(workspace_id=workspace_id, source="chapter/", chapters=chapters)

        artifact_path = workspace_path / "novel.md"
        markdown = _read_text(artifact_path).strip() if artifact_path.exists() else ""
        chapters = []
        if markdown:
            chapters.append(
                WorkspaceNovelChapter(
                    filename=artifact_path.name,
                    title=self._markdown_title(markdown) or str(workspace.get("outline_name") or "小说正文"),
                    markdown=markdown,
                )
            )
        return WorkspaceNovelChaptersContent(workspace_id=workspace_id, source="novel.md", chapters=chapters)

    def read_workspace_characters(self, workspace_id: str) -> WorkspaceCharacterContent | None:
        workspace = self._read_workspace_index().get(workspace_id)
        if workspace is None:
            return None

        workspace_path = Path(workspace["workspace_path"])
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        character_dir = workspace_path / "character"
        characters = []
        if character_dir.exists():
            for artifact_path in sorted(character_dir.glob("*.md"), key=lambda path: path.stem):
                characters.append(
                    CharacterMarkdownFile(
                        filename=artifact_path.name,
                        name=artifact_path.stem,
                        markdown=_read_text(artifact_path),
                    )
                )

        return WorkspaceCharacterContent(workspace_id=workspace_id, characters=characters)

    def bootstrap_workspace(self, workspace_id: str) -> dict | None:
        """一次读取 index 文件，批量返回 bootstrap 所需的全部数据。"""
        workspaces = self._read_workspace_index()
        workspace = workspaces.get(workspace_id)
        if workspace is None:
            return None

        workspace_path = Path(workspace["workspace_path"])
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        # threads: 只读一次 thread index
        threads_index = self._read_thread_index()
        thread_summaries = [
            self._to_thread_summary(t)
            for t in threads_index.values()
            if t["workspace_id"] == workspace_id
        ]

        # outline
        artifact_path = workspace_path / "outline.md"
        outline = WorkspaceOutlineContent(
            workspace_id=workspace_id,
            markdown=_read_text(artifact_path) if artifact_path.exists() else "",
        )

        # storyline（索引 storyline.md + 各故事线详情 storyline/*.md）
        storyline_index_path = workspace_path / "storyline.md"
        storyline_entries: list[StorylineEntry] = []
        storyline_dir = workspace_path / "storyline"
        if storyline_dir.exists():
            for ap in sorted(storyline_dir.glob("*.md"), key=lambda p: p.name):
                content = _read_text(ap).strip()
                if content:
                    storyline_entries.append(
                        StorylineEntry(
                            filename=ap.name,
                            title=ap.stem,
                            markdown=content,
                        )
                    )
        storyline = WorkspaceStorylineContent(
            workspace_id=workspace_id,
            index_markdown=_read_text(storyline_index_path) if storyline_index_path.exists() else "",
            entries=storyline_entries,
            file_count=len(storyline_entries),
        )

        # worldview
        worldview_path = workspace_path / "worldview.md"
        worldview = WorkspaceWorldviewContent(
            workspace_id=workspace_id,
            markdown=_read_text(worldview_path) if worldview_path.exists() else "",
        )

        # volume
        volume_dir = workspace_path / "volume"
        volume_chapters: list[VolumeChapter] = []
        if volume_dir.exists():
            for ap in sorted(volume_dir.glob("*.md"), key=lambda p: p.name):
                content = _read_text(ap).strip()
                if content:
                    volume_chapters.append(
                        VolumeChapter(
                            filename=ap.name,
                            title=self._volume_chapter_title(ap.name),
                            markdown=content,
                        )
                    )
        volume = WorkspaceVolumeContent(
            workspace_id=workspace_id, chapters=volume_chapters, file_count=len(volume_chapters),
        )

        # detail_outline
        detail_dir = workspace_path / "detail"
        detail_chapters: list[DetailOutlineChapter] = []
        if detail_dir.exists():
            for ap in sorted(detail_dir.glob("*.md"), key=lambda p: (p.name != "overview.md", p.name)):
                if ap.name == "evaluation.md":
                    continue
                content = _read_text(ap).strip()
                if content:
                    detail_chapters.append(
                        DetailOutlineChapter(
                            filename=ap.name,
                            title=self._detail_outline_title(ap.name),
                            markdown=content,
                        )
                    )
        detail_outline = WorkspaceDetailOutlineContent(
            workspace_id=workspace_id, chapters=detail_chapters, file_count=len(detail_chapters),
        )

        # characters
        character_dir = workspace_path / "character"
        characters: list[CharacterMarkdownFile] = []
        if character_dir.exists():
            for ap in sorted(character_dir.glob("*.md"), key=lambda p: p.stem):
                characters.append(
                    CharacterMarkdownFile(filename=ap.name, name=ap.stem, markdown=_read_text(ap)),
                )
        character_content = WorkspaceCharacterContent(workspace_id=workspace_id, characters=characters)

        # novel
        chapter_files = self._workspace_chapter_files(workspace_path)
        if chapter_files:
            novel_markdown = "\n\n".join(_read_text(p).strip() for p in chapter_files)
            novel = WorkspaceNovelContent(
                workspace_id=workspace_id,
                markdown=f"{novel_markdown}\n" if novel_markdown else "",
                source="chapter/",
                chapter_count=len(chapter_files),
            )
        else:
            novel_artifact = workspace_path / "novel.md"
            novel = WorkspaceNovelContent(
                workspace_id=workspace_id,
                markdown=_read_text(novel_artifact) if novel_artifact.exists() else "",
                source="novel.md",
                chapter_count=0,
            )

        return {
            "workspace": workspace,
            "threads": sorted(thread_summaries, key=lambda t: t.updated_at, reverse=True),
            "outline": outline,
            "storyline": storyline,
            "volume": volume,
            "detail_outline": detail_outline,
            "characters": character_content,
            "worldview": worldview,
            "novel": novel,
        }

    def write_outline(
        self,
        thread: ThreadSummary,
        response: ScreenplayGenerateResponse,
    ) -> None:
        workspace_path = Path(thread.workspace_path)
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        artifact_path = workspace_path / "outline.md"
        markdown = response.markdown.strip() or self._fallback_outline_markdown(response)
        artifact_path.write_text(f"{markdown}\n", encoding="utf-8")

        evaluation_markdown = response.evaluation_markdown.strip()
        if evaluation_markdown:
            evaluation_path = workspace_path / "evaluation.md"
            evaluation_path.write_text(f"{evaluation_markdown}\n", encoding="utf-8")

        self._touch_thread(thread.thread_id)
        self._touch_workspace(thread.workspace_id)

    def write_character(
        self,
        thread: ThreadSummary,
        response: CharacterGenerateResponse,
    ) -> None:
        workspace_path = Path(thread.workspace_path)
        if not workspace_path.exists():
            raise FileNotFoundError(f"Workspace directory missing: {workspace_path}")

        artifact_dir = workspace_path / "character"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / f"{response.name}.md"
        markdown = response.markdown.strip() or self._fallback_character_markdown(response)
        artifact_path.write_text(f"{markdown}\n", encoding="utf-8")
        self._touch_thread(thread.thread_id)
        self._touch_workspace(thread.workspace_id)

    def _detail_outline_title(self, filename: str) -> str:
        """从文件名推导显示标题：overview.md → 总览，chapter-01.md → 第1章"""
        if filename == "overview.md":
            return "总览"
        match = re.match(r"chapter-(\d+)\.md$", filename)
        if match:
            return f"第{int(match.group(1))}章"
        return Path(filename).stem

    def _workspace_chapter_files(self, workspace_path: Path) -> list[Path]:
        chapter_dir = workspace_path / "chapter"
        if not chapter_dir.exists():
            return []
        return sorted(
            (
                path
                for path in chapter_dir.glob("*.md")
                if path.is_file() and _read_text(path).strip()
            ),
            key=lambda path: path.name,
        )

    def _markdown_title(self, markdown: str) -> str:
        for line in markdown.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
            if stripped:
                return stripped
        return ""

    def _fallback_character_markdown(
        self,
        response: CharacterGenerateResponse,
    ) -> str:
        return (
            f"# {response.name}\n\n"
            f"## 角色身份\n\n{response.identity}\n\n"
            f"## 外貌特征\n\n{response.appearance}\n\n"
            f"## 性格与内心\n\n{response.personality}\n\n"
            f"## 关系网络\n\n{response.relationships}\n\n"
            f"## 目前状态\n\n{response.current_state}\n"
        )

    def _fallback_outline_markdown(
        self,
        response: ScreenplayGenerateResponse,
    ) -> str:
        beat_lines = "\n".join(
            f"{index}. {beat}" for index, beat in enumerate(response.beats, start=1)
        )
        return (
            f"# {response.title}\n\n"
            f"## 一句话梗概\n\n{response.logline}\n\n"
            f"## 短梗概\n\n{response.synopsis}\n\n"
            f"## 五个关键剧情节点\n\n{beat_lines}"
        )

    def _migrate_legacy_indexes(self) -> None:
        if self.workspace_index_path.exists() and self.thread_index_path.exists():
            threads = self._read_thread_index()
            if all("workspace_id" in thread for thread in threads.values()):
                return

        if not self.thread_index_path.exists():
            return

        threads = self._read_raw_index(self.thread_index_path)
        if not threads:
            return
        if any("workspace_id" in thread for thread in threads.values()):
            return

        migrated_workspaces: dict[str, dict[str, str]] = {}
        migrated_threads: dict[str, dict[str, str]] = {}
        for thread_id, legacy_thread in threads.items():
            workspace_id = str(legacy_thread["thread_id"])
            workspace_path = str(legacy_thread["workspace_path"])
            outline_name = str(legacy_thread["outline_name"])
            created_at = str(legacy_thread["created_at"])
            updated_at = str(legacy_thread["updated_at"])
            migrated_workspaces[workspace_id] = {
                "workspace_id": workspace_id,
                "outline_name": outline_name,
                "workspace_path": workspace_path,
                "created_at": created_at,
                "updated_at": updated_at,
                "session_count": 1,
            }
            migrated_threads[thread_id] = {
                "thread_id": thread_id,
                "workspace_id": workspace_id,
                "session_name": outline_name,
                "workspace_path": workspace_path,
                "created_at": created_at,
                "updated_at": updated_at,
            }

        self._write_workspace_index(migrated_workspaces)
        self._write_thread_index(migrated_threads)

    def _read_workspace_index(self) -> dict[str, dict[str, str]]:
        return self._read_raw_index(self.workspace_index_path)

    def _read_thread_index(self) -> dict[str, dict[str, str]]:
        return self._read_raw_index(self.thread_index_path)

    def _read_raw_index(self, path: Path) -> dict[str, dict[str, str]]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError(f"Invalid index format: {path}")
        return data

    def _write_workspace_index(self, index: dict[str, dict[str, str]]) -> None:
        self._write_json(self.workspace_index_path, index)

    def _write_thread_index(self, index: dict[str, dict[str, str]]) -> None:
        self._write_json(self.thread_index_path, index)

    def _write_json(self, path: Path, value: dict[str, dict[str, str]]) -> None:
        with path.open("w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)

    def _touch_workspace(self, workspace_id: str) -> None:
        workspaces = self._read_workspace_index()
        workspace = workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(f"Workspace not found: {workspace_id}")
        workspace["updated_at"] = self._now()
        workspace["session_count"] = sum(
            1 for thread in self._read_thread_index().values() if thread["workspace_id"] == workspace_id
        )
        workspaces[workspace_id] = workspace
        self._write_workspace_index(workspaces)

    def _touch_thread(self, thread_id: str) -> None:
        threads = self._read_thread_index()
        thread = threads.get(thread_id)
        if thread is None:
            raise KeyError(f"Thread not found: {thread_id}")
        thread["updated_at"] = self._now()
        threads[thread_id] = thread
        self._write_thread_index(threads)

    def clear_workspace_style_reference(self, style_id: str) -> None:
        workspaces = self._read_workspace_index()
        changed = False
        for workspace in workspaces.values():
            if workspace.get("active_style_id") == style_id:
                workspace["active_style_id"] = None
                changed = True
        if changed:
            self._write_workspace_index(workspaces)

    def _to_workspace_summary(self, workspace: dict[str, str | int]) -> WorkspaceSummary:
        workspace.setdefault("active_style_id", None)
        return WorkspaceSummary(**workspace)

    def _to_thread_summary(self, thread: dict[str, str]) -> ThreadSummary:
        return ThreadSummary(**thread)

    def _workspace_dir(self, workspace_id: str) -> Path:
        return self.workspace_root / workspace_id

    def _make_thread_id(self, workspace_id: str, session_name: str) -> str:
        slug = self._sanitize_thread_name(session_name)[:24] or "session"
        return f"{workspace_id[:12]}-{slug}-{uuid4().hex[:8]}"

    def _sanitize_workspace_name(self, name: str) -> str:
        sanitized = self._sanitize_path_name(name)
        if not sanitized:
            raise ValueError("outline_name cannot be empty")
        return sanitized

    def _sanitize_thread_name(self, name: str) -> str:
        return self._sanitize_path_name(name).lower()

    def _sanitize_path_name(self, name: str) -> str:
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", name.strip()).strip(".")

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()
