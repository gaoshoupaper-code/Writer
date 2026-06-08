from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceCreateRequest(BaseModel):
    outline_name: str = Field(min_length=1)


class WorkspaceSummary(BaseModel):
    workspace_id: str
    outline_name: str
    workspace_path: str
    created_at: str
    updated_at: str
    session_count: int = 0
    active_style_id: str | None = None


class ThreadCreateRequest(BaseModel):
    workspace_id: str
    session_name: str | None = None


class ThreadUpdateRequest(BaseModel):
    session_name: str = Field(min_length=1)


class ThreadSummary(BaseModel):
    thread_id: str
    workspace_id: str
    session_name: str
    workspace_path: str
    created_at: str
    updated_at: str


class WorkspaceOutlineContent(BaseModel):
    workspace_id: str
    markdown: str


class DetailOutlineChapter(BaseModel):
    filename: str
    title: str
    markdown: str


class WorkspaceDetailOutlineContent(BaseModel):
    workspace_id: str
    chapters: list[DetailOutlineChapter]
    file_count: int = 0


class WorkspaceNovelContent(BaseModel):
    workspace_id: str
    markdown: str
    source: str = "novel.md"
    chapter_count: int = 0


class WorkspaceNovelChapter(BaseModel):
    filename: str
    title: str
    markdown: str


class WorkspaceNovelChaptersContent(BaseModel):
    workspace_id: str
    source: str = "novel.md"
    chapters: list[WorkspaceNovelChapter]


class CharacterMarkdownFile(BaseModel):
    filename: str
    name: str
    markdown: str


class WorkspaceCharacterContent(BaseModel):
    workspace_id: str
    characters: list[CharacterMarkdownFile]


class ScreenplayGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str | None = None
    content: str | None = None
    text: str | None = None
    title: str | None = None
    genre: str | None = None
    premise: str | None = None
    tone: str | None = None
    audience: str | None = None
    thread_id: str

    def primary_text(self) -> str:
        return self.prompt or self.content or self.text or self.premise or ""

    def fallback_title(self) -> str:
        return self.title or "未命名大纲"

    def loose_context(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, exclude={"thread_id"})


class ScreenplayGenerateResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    mode: str
    thread_id: str
    workspace_id: str
    session_name: str
    workspace_path: str
    title: str
    content: str
    logline: str = ""
    synopsis: str = ""
    beats: list[str] = Field(default_factory=list)
    markdown: str = ""
    evaluation_markdown: str = ""
