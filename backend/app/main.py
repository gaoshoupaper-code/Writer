import json
import re
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from watchfiles import awatch

from docx import Document
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from app.writer.expert_agent.services.character import CharacterService
from app.writer.meta import MetaAgentService
from app.core.settings import get_settings
from app.create_type.store import CreateTypeStore
from app.create_type.optimizer import StyleOptimizer
from app.create_type.router import init_style_module, router as style_router
from app.core.thread_store import ThreadStore
from app.writer.trace import TraceRecorder
from app.schemas.character import CharacterGenerateRequest, CharacterGenerateResponse
from app.schemas.screenplay import (
    InitResponse,
    ScreenplayGenerateRequest,
    ScreenplayGenerateResponse,
    ThreadCreateRequest,
    ThreadSummary,
    ThreadUpdateRequest,
    WorkspaceBootstrapResponse,
    WorkspaceCharacterContent,
    WorkspaceCreateRequest,
    WorkspaceDetailOutlineContent,
    WorkspaceNovelChaptersContent,
    WorkspaceNovelContent,
    WorkspaceOutlineContent,
    WorkspaceStorylineContent,
    WorkspaceStorylineGraphContent,
    WorkspaceVolumeContent,
    WorkspaceWorldviewContent,
    WorkspaceSummary,
)
from app.schemas.checkpoint import CheckpointState
from app.writer.trace import TraceDetail, TraceRunSummary


def _markdown_to_plain_text(markdown: str) -> str:
    lines = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            lines.append(stripped[3:].strip())
        elif stripped.startswith("# "):
            lines.append(stripped[2:].strip())
        elif stripped == "---":
            lines.append("")
        else:
            lines.append(line)
    return "\n".join(lines).strip()


def _escape_pdf_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _safe_download_name(name: str, fallback: str, max_length: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().strip(".")
    return (cleaned or fallback)[:max_length]


def _build_novel_docx(markdown: str, title: str) -> bytes:
    document = Document()
    document.add_heading(title, level=1)
    for block in _markdown_to_plain_text(markdown).split("\n\n"):
        text = block.strip()
        if not text or text == title:
            continue
        document.add_paragraph(text)

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _build_novel_docx_zip(content: WorkspaceNovelChaptersContent) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, chapter in enumerate(content.chapters, start=1):
            title = chapter.title.strip() or Path(chapter.filename).stem or f"chapter-{index:03d}"
            safe_title = _safe_download_name(title, f"chapter-{index:03d}")
            archive.writestr(f"{index:03d}-{safe_title}.docx", _build_novel_docx(chapter.markdown, title))
    return buffer.getvalue()


def _build_novel_pdf(markdown: str, title: str) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
        title=title,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "NovelTitle",
        parent=styles["Title"],
        fontName="STSong-Light",
        fontSize=20,
        leading=28,
        spaceAfter=24,
    )
    chapter_style = ParagraphStyle(
        "ChapterTitle",
        parent=styles["Heading2"],
        fontName="STSong-Light",
        fontSize=15,
        leading=22,
        spaceBefore=8,
        spaceAfter=12,
    )
    body_style = ParagraphStyle(
        "NovelBody",
        parent=styles["BodyText"],
        fontName="STSong-Light",
        fontSize=11,
        leading=19,
        firstLineIndent=22,
        spaceAfter=7,
    )

    story = [Paragraph(_escape_pdf_text(title), title_style)]
    first_chapter = True
    for block in _markdown_to_plain_text(markdown).split("\n\n"):
        text = block.strip()
        if not text:
            continue
        if text.startswith("第") and "章" in text[:8]:
            if not first_chapter:
                story.append(PageBreak())
            first_chapter = False
            story.append(Paragraph(_escape_pdf_text(text), chapter_style))
            continue
        story.append(Paragraph(_escape_pdf_text(text).replace("\n", "<br/>"), body_style))
        story.append(Spacer(1, 4))

    doc.build(story)
    return buffer.getvalue()


async def _event_generator(payload: ScreenplayGenerateRequest, thread: ThreadSummary):
    """Async generator that yields SSE events from the agent execution."""
    final_data = None
    async for chunk in agent_service.generate_stream(payload, thread):
        yield chunk
        if chunk.startswith("event: final"):
            for line in chunk.split("\n"):
                if line.startswith("data: "):
                    final_data = line[6:]
                    break
    if final_data:
        import json

        response = ScreenplayGenerateResponse.model_validate(json.loads(final_data))
        thread_store.write_outline(thread, response)


settings = get_settings()
workspace_root = Path(__file__).resolve().parents[1] / "workspace"
trace_recorder = TraceRecorder()
thread_store = ThreadStore(workspace_root)
style_store = CreateTypeStore(workspace_root, thread_store)
style_optimizer = StyleOptimizer(settings)
init_style_module(style_store, style_optimizer)

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

checkpoint_db_path = workspace_root.parent / "checkpoints.db"
_checkpointer_cm = AsyncSqliteSaver.from_conn_string(str(checkpoint_db_path))

agent_service: MetaAgentService | None = None
character_service: CharacterService | None = None


async def _lifespan(application: FastAPI):
    global agent_service, character_service
    checkpointer = await _checkpointer_cm.__aenter__()
    if agent_service is None:
        agent_service = MetaAgentService(settings, workspace_root, trace_recorder, style_store, checkpointer)
    if character_service is None:
        character_service = CharacterService(settings, workspace_root, trace_recorder, checkpointer)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    yield
    await _checkpointer_cm.__aexit__(None, None, None)


app = FastAPI(
    title="Writer Agent API",
    version="0.1.0",
    description="Minimal screenplay generation backend built with DeepAgents and FastAPI.",
    lifespan=_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.writer_frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(style_router)


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok", "mode": settings.writer_agent_mode}


@app.get("/api/init", response_model=InitResponse)
def init_page() -> InitResponse:
    """页面首次加载：一次返回 workspaces + styles，替代 2 个独立请求。"""
    return InitResponse(
        workspaces=thread_store.list_workspaces(),
        styles=style_store.list_styles(),
    )


@app.get("/api/workspaces/{workspace_id}/bootstrap", response_model=WorkspaceBootstrapResponse)
def bootstrap_workspace(workspace_id: str) -> WorkspaceBootstrapResponse:
    """选中工作区后：一次返回 threads + 全部面板内容，替代 5 个独立请求。"""
    data = thread_store.bootstrap_workspace(workspace_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    return WorkspaceBootstrapResponse(
        threads=data["threads"],
        outline=data["outline"],
        storyline=data["storyline"],
        volume=data["volume"],
        detail_outline=data["detail_outline"],
        characters=data["characters"],
        novel=data["novel"],
        worldview=data["worldview"],
    )


@app.get("/api/workspaces", response_model=list[WorkspaceSummary])
def list_workspaces() -> list[WorkspaceSummary]:
    return thread_store.list_workspaces()


@app.post("/api/workspaces", response_model=WorkspaceSummary)
def create_workspace(payload: WorkspaceCreateRequest) -> WorkspaceSummary:
    try:
        return thread_store.create_workspace(payload.outline_name)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/workspaces/{workspace_id}/outline", response_model=WorkspaceOutlineContent)
def get_workspace_outline(workspace_id: str) -> WorkspaceOutlineContent:
    content = thread_store.read_workspace_outline(workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/storyline", response_model=WorkspaceStorylineContent)
def get_workspace_storyline(workspace_id: str) -> WorkspaceStorylineContent:
    content = thread_store.read_workspace_storyline(workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/detail-outline", response_model=WorkspaceDetailOutlineContent)
def get_workspace_detail_outline(workspace_id: str) -> WorkspaceDetailOutlineContent:
    content = thread_store.read_workspace_detail_outline(workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/worldview", response_model=WorkspaceWorldviewContent)
def get_workspace_worldview(workspace_id: str) -> WorkspaceWorldviewContent:
    content = thread_store.read_workspace_worldview(workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/volume", response_model=WorkspaceVolumeContent)
def get_workspace_volume(workspace_id: str) -> WorkspaceVolumeContent:
    content = thread_store.read_workspace_volume(workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/storyline-graph", response_model=WorkspaceStorylineGraphContent)
def get_workspace_storyline_graph(workspace_id: str) -> WorkspaceStorylineGraphContent:
    """故事线流程图（竖向泳道时间轴）。读取时按需生成兜底——图缺失/过期自动重生成。"""
    content = thread_store.read_workspace_storyline_graph(workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/characters", response_model=WorkspaceCharacterContent)
def get_workspace_characters(workspace_id: str) -> WorkspaceCharacterContent:
    content = thread_store.read_workspace_characters(workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/novel", response_model=WorkspaceNovelContent)
def get_workspace_novel(workspace_id: str) -> WorkspaceNovelContent:
    content = thread_store.read_workspace_novel(workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


def _sse_event(event_type: str, payload: object) -> str:
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {data}\n\n"


def _classify_changes(changes, workspace_path: Path) -> set[str]:
    categories: set[str] = set()
    for _change_type, path_str in changes:
        try:
            rel = Path(path_str).relative_to(workspace_path)
        except ValueError:
            continue
        parts = rel.parts
        if not parts:
            continue
        top = parts[0]
        if top in ("outline.md", "evaluation.md"):
            categories.add("outline")
        elif top == "storyline.md" or (len(parts) > 1 and parts[0] == "storyline"):
            categories.add("storyline")
        elif top == "worldview.md":
            categories.add("worldview")
        elif len(parts) > 1 and parts[0] == "volume":
            categories.add("volume")
        elif len(parts) > 1 and parts[0] == "detail":
            categories.add("detail_outline")
        elif len(parts) > 1 and parts[0] == "character":
            categories.add("characters")
        elif len(parts) > 1 and parts[0] == "chapter":
            categories.add("novel")
        elif top == "novel.md":
            categories.add("novel")
    return categories


async def _workspace_watch_generator(workspace_id: str, workspace_path: Path):
    async for changes in awatch(
        workspace_path,
        watch_filter=lambda _change, path: Path(path).suffix == ".md",
        debounce=400,
        step=50,
        recursive=True,
        ignore_permission_denied=True,
    ):
        categories = _classify_changes(changes, workspace_path)
        if not categories:
            continue
        if "outline" in categories:
            content = thread_store.read_workspace_outline(workspace_id)
            if content is not None:
                yield _sse_event("outline", content.model_dump())
        if "storyline" in categories:
            content = thread_store.read_workspace_storyline(workspace_id)
            if content is not None:
                yield _sse_event("storyline", content.model_dump())
        if "worldview" in categories:
            content = thread_store.read_workspace_worldview(workspace_id)
            if content is not None:
                yield _sse_event("worldview", content.model_dump())
        if "volume" in categories:
            content = thread_store.read_workspace_volume(workspace_id)
            if content is not None:
                yield _sse_event("volume", content.model_dump())
        if "detail_outline" in categories:
            content = thread_store.read_workspace_detail_outline(workspace_id)
            if content is not None:
                yield _sse_event("detail_outline", content.model_dump())
        if "characters" in categories:
            content = thread_store.read_workspace_characters(workspace_id)
            if content is not None:
                yield _sse_event("characters", content.model_dump())
        if "novel" in categories:
            content = thread_store.read_workspace_novel(workspace_id)
            if content is not None:
                yield _sse_event("novel", content.model_dump())


@app.get("/api/workspaces/{workspace_id}/watch")
async def watch_workspace(workspace_id: str):
    workspace = thread_store.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    workspace_path = Path(workspace.workspace_path)
    if not workspace_path.exists():
        raise HTTPException(status_code=404, detail="Workspace directory missing")
    return StreamingResponse(
        _workspace_watch_generator(workspace_id, workspace_path),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/workspaces/{workspace_id}/novel/export.pdf")
def export_workspace_novel_pdf(workspace_id: str) -> Response:
    workspace = thread_store.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    content = thread_store.read_workspace_novel(workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not content.markdown.strip():
        raise HTTPException(status_code=404, detail="Novel content not found")

    filename = f"{workspace.outline_name or workspace_id}.pdf"
    pdf = _build_novel_pdf(content.markdown, workspace.outline_name or "小说正文")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/api/workspaces/{workspace_id}/novel/export-word.zip")
def export_workspace_novel_word_zip(workspace_id: str) -> Response:
    workspace = thread_store.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    content = thread_store.read_workspace_novel_chapters(workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not content.chapters:
        raise HTTPException(status_code=404, detail="Novel content not found")

    filename_base = _safe_download_name(workspace.outline_name or workspace_id, workspace_id)
    archive = _build_novel_docx_zip(content)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f'{filename_base}-word.zip')}"},
    )


@app.delete("/api/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str) -> dict[str, str | bool | list[str]]:
    try:
        deleted_thread_ids = thread_store.delete_workspace(workspace_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if deleted_thread_ids is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    for thread_id in deleted_thread_ids:
        agent_service.delete_thread_checkpoint(thread_id)
        character_service.delete_thread_checkpoint(thread_id)
    return {"status": "ok", "deleted": workspace_id, "deleted_threads": deleted_thread_ids}


@app.get("/api/threads", response_model=list[ThreadSummary])
def list_threads(workspace_id: str | None = None) -> list[ThreadSummary]:
    return thread_store.list_threads(workspace_id)


@app.post("/api/threads", response_model=ThreadSummary)
def create_thread(payload: ThreadCreateRequest) -> ThreadSummary:
    try:
        return thread_store.create_thread(payload.workspace_id, payload.session_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/threads/{thread_id}", response_model=ThreadSummary)
def update_thread(thread_id: str, payload: ThreadUpdateRequest) -> ThreadSummary:
    try:
        thread = thread_store.update_thread_name(thread_id, payload.session_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread


@app.delete("/api/threads/{thread_id}")
def delete_thread(thread_id: str) -> dict[str, str | bool]:
    thread = thread_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    try:
        trace_recorder.delete_thread_runs(thread)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    deleted = thread_store.delete_thread(thread_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found")
    agent_service.delete_thread_checkpoint(thread_id)
    character_service.delete_thread_checkpoint(thread_id)
    return {"status": "ok", "deleted": thread_id}


@app.get("/api/threads/{thread_id}/outline", response_model=WorkspaceOutlineContent)
def get_thread_outline(thread_id: str) -> WorkspaceOutlineContent:
    content = thread_store.read_thread_outline(thread_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return content


@app.get("/api/threads/{thread_id}/checkpoint", response_model=CheckpointState)
async def get_thread_checkpoint(thread_id: str) -> CheckpointState:
    thread = thread_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return await agent_service.get_thread_checkpoint(thread_id)


@app.get("/api/threads/{thread_id}/traces", response_model=list[TraceRunSummary])
def list_thread_traces(thread_id: str) -> list[TraceRunSummary]:
    thread = thread_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return trace_recorder.list_runs(thread)


@app.get("/api/threads/{thread_id}/traces/{trace_id}", response_model=TraceDetail)
def get_thread_trace(thread_id: str, trace_id: str) -> TraceDetail:
    thread = thread_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    try:
        detail = trace_recorder.read_run(thread, trace_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return detail


@app.delete("/api/threads/{thread_id}/traces/{trace_id}")
def delete_thread_trace(thread_id: str, trace_id: str) -> dict[str, str]:
    thread = thread_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    try:
        deleted = trace_recorder.delete_run(thread, trace_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"status": "ok", "deleted": trace_id}


@app.post("/api/screenplay/generate/stream")
async def stream_screenplay(payload: ScreenplayGenerateRequest):
    thread = thread_store.get_thread(payload.thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return StreamingResponse(
        _event_generator(payload, thread),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/character/generate", response_model=CharacterGenerateResponse)
def generate_character(payload: CharacterGenerateRequest) -> CharacterGenerateResponse:
    thread = thread_store.get_thread(payload.thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    response = character_service.generate(payload, thread)
    thread_store.write_character(thread, response)
    return response
