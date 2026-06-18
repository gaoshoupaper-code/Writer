import json
import re
import time
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from watchfiles import awatch

from docx import Document
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
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
from app.platform.trace import TraceRecorder
from app.schemas.character import CharacterGenerateRequest, CharacterGenerateResponse
from app.schemas.screenplay import (
    InitResponse,
    ScreenplayGenerateRequest,
    ScreenplayGenerateResponse,
    StorylineGraphEvent,
    StorylineGraphStoryline,
    ThreadCreateRequest,
    ThreadSummary,
    ThreadUpdateRequest,
    WorkspaceBootstrapResponse,
    WorkspaceCharacterContent,
    WorkspaceCreateRequest,
    WorkspaceDetailOutlineContent,
    WorkspaceNovelChaptersContent,
    WorkspaceOutlineContent,
    WorkspaceStorylineContent,
    WorkspaceStorylineGraphContent,
    WorkspaceWorldviewContent,
    WorkspaceSummary,
)
from app.schemas.checkpoint import CheckpointState
from app.platform.trace import TraceDetail, TraceRunSummary
from app.platform.agent.middleware import TraceCallbackHandler
from app.auth import auth_router, current_user, CurrentUser
from app.auth.bootstrap import bootstrap_admin
from app.admin import me_router, admin_router
from app.core.checkpoint_pool import CheckpointPool, init_checkpoint_pool, get_checkpoint_pool
from app.core.security import load_master_key
from app.db import Database, init_database, get_database, UserRepository


_active_generations = 0


def _log(event: str, **fields) -> None:
    """诊断日志：结构化 JSON 到 stdout（flush 保证即时输出）。

    为 Phase 1 根因诊断服务——定位 SSE 连接泄漏 / 请求挂起 / 锁竞争。
    设计文档 §5 约定：轻量、零依赖、JSON 到 stdout。
    """
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    print(json.dumps(record, ensure_ascii=False), flush=True)


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


async def _event_generator(payload: ScreenplayGenerateRequest, thread: ThreadSummary, *, owner_id: str | None = None):
    """Async generator that yields SSE events from the agent execution."""
    global _active_generations
    _active_generations += 1
    _log("sse_open", channel="generate", thread_id=payload.thread_id, active=_active_generations)
    start = time.perf_counter()
    final_data = None
    try:
        async for chunk in agent_service.generate_stream(payload, thread, owner_id=owner_id):
            yield chunk
            if chunk.startswith("event: final"):
                for line in chunk.split("\n"):
                    if line.startswith("data: "):
                        final_data = line[6:]
                        break
        if final_data:
            import json

            response = ScreenplayGenerateResponse.model_validate(json.loads(final_data))
            # write_outline 需要 owner_id 来定位工作区路径（thread.workspace_path 已含）
            thread_store.write_outline(owner_id or "", thread, response)
        _log("sse_close", channel="generate", thread_id=payload.thread_id,
             ms=int((time.perf_counter() - start) * 1000))
    except BaseException as exc:
        _log("sse_error", channel="generate", thread_id=payload.thread_id,
             error=type(exc).__name__, ms=int((time.perf_counter() - start) * 1000))
        raise
    finally:
        _active_generations -= 1
        _log("sse_exit", channel="generate", thread_id=payload.thread_id, active=_active_generations)


settings = get_settings()
workspace_root = Path(__file__).resolve().parents[1] / settings.workspace_root
trace_recorder = TraceRecorder()

# 多用户地基：元数据库 + checkpoint 分库池
_master_key = load_master_key(settings.master_key)
_database = Database(settings.db_path, _master_key)
init_database(_database)

_checkpoints_root = Path(__file__).resolve().parents[1] / "checkpoints"
_checkpoint_pool = CheckpointPool(_checkpoints_root)
init_checkpoint_pool(_checkpoint_pool)

thread_store = ThreadStore(_database, workspace_root)
style_store = CreateTypeStore(_database, thread_store.workspaces)
style_optimizer = StyleOptimizer(settings)
init_style_module(style_store, style_optimizer)

# 全局 checkpointer：仅作管理员兜底与同步 delete_thread 路径用；
# 普通用户走 CheckpointPool 分库（services 内部按 owner_id 解析）。
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

_global_checkpoint_db_path = workspace_root.parent / "checkpoints.db"
_checkpointer_cm = AsyncSqliteSaver.from_conn_string(str(_global_checkpoint_db_path))

agent_service: MetaAgentService | None = None
character_service: CharacterService | None = None
image_agent_service = None  # ImageAgentService 实例（Phase 3）


async def _lifespan(application: FastAPI):
    global agent_service, character_service, image_agent_service
    checkpointer = await _checkpointer_cm.__aenter__()
    if agent_service is None:
        agent_service = MetaAgentService(settings, workspace_root, trace_recorder, style_store, checkpointer)
    if character_service is None:
        character_service = CharacterService(settings, workspace_root, trace_recorder, checkpointer)
    if image_agent_service is None:
        from app.domains.image.agent import ImageAgentService
        image_agent_service = ImageAgentService(settings, workspace_root, trace_recorder, checkpointer)
        from app.domains.image.router import init_image_routes
        init_image_routes(image_agent_service, thread_store)
    # 多用户：引导管理员账号（幂等）
    bootstrap_admin()
    # 启动 trace 写盘 drain 协程：append_event 不再同步写盘，改入内存缓冲，
    # 由此后台协程成批落盘（to_thread，不占事件循环）。
    trace_recorder.start_drain()
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    yield
    # 关闭 drain 并刷掉残余事件，保证进程退出前 trace 数据完整。
    await trace_recorder.aclose()
    await _checkpointer_cm.__aexit__(None, None, None)
    await _checkpoint_pool.aclose_all()


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
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(admin_router)
# image domain 路由（Phase 3：图片服务端点 + Skill 管理）
from app.domains.image.router import router as image_router
app.include_router(image_router)


@app.middleware("http")
async def _log_http(request: Request, call_next):
    """记录每个 HTTP 请求的耗时与状态码。

    注意：对 SSE（StreamingResponse），Starlette 中间件的 ms ≈ 首字节时间，
    非连接全程时长——后者由 _event_generator / _workspace_watch_generator
    内部的 sse_close/sse_error 埋点覆盖。中间件主职是普通短请求（fetchInit 等）的耗时。
    """
    start = time.perf_counter()
    status_code = 0
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        _log("http", method=request.method, path=request.url.path,
             status=status_code, ms=int((time.perf_counter() - start) * 1000))


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok", "mode": settings.writer_agent_mode}


@app.get("/api/init", response_model=InitResponse)
def init_page(user: CurrentUser = Depends(current_user)) -> InitResponse:
    """页面首次加载：一次返回当前用户的 workspaces + styles。"""
    return InitResponse(
        workspaces=thread_store.list_workspaces(user.user_id),
        styles=style_store.list_styles(user.user_id),
    )


@app.get("/api/workspaces/{workspace_id}/bootstrap", response_model=WorkspaceBootstrapResponse)
def bootstrap_workspace(workspace_id: str, user: CurrentUser = Depends(current_user)) -> WorkspaceBootstrapResponse:
    """选中工作区后：一次返回 threads + 全部面板内容，替代 5 个独立请求。"""
    data = thread_store.bootstrap_workspace(user.user_id, workspace_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    return WorkspaceBootstrapResponse(
        threads=data["threads"],
        outline=data["outline"],
        storyline=data["storyline"],
        detail_outline=data["detail_outline"],
        characters=data["characters"],
        novel=data["novel"],
        worldview=data["worldview"],
    )


@app.get("/api/workspaces", response_model=list[WorkspaceSummary])
def list_workspaces(user: CurrentUser = Depends(current_user)) -> list[WorkspaceSummary]:
    return thread_store.list_workspaces(user.user_id)


@app.post("/api/workspaces", response_model=WorkspaceSummary)
def create_workspace(payload: WorkspaceCreateRequest, user: CurrentUser = Depends(current_user)) -> WorkspaceSummary:
    # 配额检查（T2.8）
    users = UserRepository(get_database())
    owner = users.get_by_id(user.user_id)
    quota = owner["workspace_quota"] if owner else settings.default_workspace_quota
    if users.workspace_count(user.user_id) >= quota:
        raise HTTPException(
            status_code=409,
            detail=f"作品数量已达上限（{quota} 部）",
        )
    try:
        return thread_store.create_workspace(user.user_id, payload.title, payload.domain)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/workspaces/{workspace_id}/outline", response_model=WorkspaceOutlineContent)
def get_workspace_outline(workspace_id: str, user: CurrentUser = Depends(current_user)) -> WorkspaceOutlineContent:
    content = thread_store.read_workspace_outline(user.user_id, workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/storyline", response_model=WorkspaceStorylineContent)
def get_workspace_storyline(workspace_id: str, user: CurrentUser = Depends(current_user)) -> WorkspaceStorylineContent:
    content = thread_store.read_workspace_storyline(user.user_id, workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/detail-outline", response_model=WorkspaceDetailOutlineContent)
def get_workspace_detail_outline(workspace_id: str, user: CurrentUser = Depends(current_user)) -> WorkspaceDetailOutlineContent:
    content = thread_store.read_workspace_detail_outline(user.user_id, workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/worldview", response_model=WorkspaceWorldviewContent)
def get_workspace_worldview(workspace_id: str, user: CurrentUser = Depends(current_user)) -> WorkspaceWorldviewContent:
    content = thread_store.read_workspace_worldview(user.user_id, workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/storyline-graph", response_model=WorkspaceStorylineGraphContent)
def get_workspace_storyline_graph(workspace_id: str, user: CurrentUser = Depends(current_user)) -> WorkspaceStorylineGraphContent:
    """故事线流程图（竖向泳道时间轴）。读取时按需生成兜底——图缺失/过期自动重生成。

    生成逻辑在 API 层（而非 thread_store）：避免 core（基础设施层）反向依赖 writer。
    thread_store 只负责读 markdown，结构化数据（events/storylines/t_map）在此解析填充。
    """
    content = thread_store.read_workspace_storyline_graph(user.user_id, workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # 按需生成兜底：图缺失/过期 → 重生成（storyline_graph 是派生视图，幂等安全）
    from app.writer.expert_agent.services.storyline_graph import (
        build_storyline_graph_data,
        generate_storyline_graph,
        is_stale,
    )
    workspace = thread_store.get_workspace(user.user_id, workspace_id)
    if workspace is not None:
        ws_path = Path(workspace.workspace_path)
        stale = is_stale(ws_path)
        if stale:
            generate_storyline_graph(ws_path)
            content = thread_store.read_workspace_storyline_graph(user.user_id, workspace_id)
            if content is None:
                raise HTTPException(status_code=404, detail="Workspace not found")
            content.stale = True
        # 填充结构化数据（reactflow 自定义布局用）
        data = build_storyline_graph_data(ws_path)
        if data is not None:
            content.storylines = [
                StorylineGraphStoryline(
                    id=sl.id, name=sl.name, type=sl.type, status=sl.status,
                    direction=sl.direction, key_events=sl.key_events,
                ) for sl in data.storylines
            ]
            content.events = {
                eid: StorylineGraphEvent(
                    id=ev.id, name=ev.name, type=ev.type,
                    storylines=list(ev.storylines), group=ev.group, doc_order=ev.doc_order,
                ) for eid, ev in data.events.items()
            }
            content.t_map = dict(data.t_map)
            content.storyline_count = len(data.storylines)
            content.event_count = len(data.events)
    return content


@app.get("/api/workspaces/{workspace_id}/characters", response_model=WorkspaceCharacterContent)
def get_workspace_characters(workspace_id: str, user: CurrentUser = Depends(current_user)) -> WorkspaceCharacterContent:
    content = thread_store.read_workspace_characters(user.user_id, workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return content


@app.get("/api/workspaces/{workspace_id}/novel", response_model=WorkspaceNovelChaptersContent)
def get_workspace_novel(workspace_id: str, user: CurrentUser = Depends(current_user)) -> WorkspaceNovelChaptersContent:
    content = thread_store.read_workspace_novel_chapters(user.user_id, workspace_id)
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
        elif len(parts) > 1 and parts[0] == "detail":
            categories.add("detail_outline")
        elif len(parts) > 1 and parts[0] == "character":
            categories.add("characters")
        elif len(parts) > 1 and parts[0] == "chapter":
            categories.add("novel")
        elif top == "novel.md":
            categories.add("novel")
    return categories


async def _workspace_watch_generator(owner_id: str, workspace_id: str, workspace_path: Path):
    _log("sse_open", channel="watch", workspace_id=workspace_id)
    start = time.perf_counter()
    try:
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
                content = thread_store.read_workspace_outline(owner_id, workspace_id)
                if content is not None:
                    yield _sse_event("outline", content.model_dump())
            if "storyline" in categories:
                content = thread_store.read_workspace_storyline(owner_id, workspace_id)
                if content is not None:
                    yield _sse_event("storyline", content.model_dump())
            if "worldview" in categories:
                content = thread_store.read_workspace_worldview(owner_id, workspace_id)
                if content is not None:
                    yield _sse_event("worldview", content.model_dump())
            if "detail_outline" in categories:
                content = thread_store.read_workspace_detail_outline(owner_id, workspace_id)
                if content is not None:
                    yield _sse_event("detail_outline", content.model_dump())
            if "characters" in categories:
                content = thread_store.read_workspace_characters(owner_id, workspace_id)
                if content is not None:
                    yield _sse_event("characters", content.model_dump())
            if "novel" in categories:
                content = thread_store.read_workspace_novel_chapters(owner_id, workspace_id)
                if content is not None:
                    yield _sse_event("novel", content.model_dump())
        _log("sse_close", channel="watch", workspace_id=workspace_id,
             ms=int((time.perf_counter() - start) * 1000))
    except BaseException as exc:
        _log("sse_error", channel="watch", workspace_id=workspace_id,
             error=type(exc).__name__, ms=int((time.perf_counter() - start) * 1000))
        raise


@app.get("/api/workspaces/{workspace_id}/watch")
async def watch_workspace(workspace_id: str, user: CurrentUser = Depends(current_user)):
    workspace = thread_store.get_workspace(user.user_id, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    workspace_path = Path(workspace.workspace_path)
    if not workspace_path.exists():
        raise HTTPException(status_code=404, detail="Workspace directory missing")
    return StreamingResponse(
        _workspace_watch_generator(user.user_id, workspace_id, workspace_path),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/workspaces/{workspace_id}/novel/export.pdf")
def export_workspace_novel_pdf(workspace_id: str, user: CurrentUser = Depends(current_user)) -> Response:
    workspace = thread_store.get_workspace(user.user_id, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    content = thread_store.read_workspace_novel(user.user_id, workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not content.markdown.strip():
        raise HTTPException(status_code=404, detail="Novel content not found")

    filename = f"{workspace.title or workspace_id}.pdf"
    pdf = _build_novel_pdf(content.markdown, workspace.title or "小说正文")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/api/workspaces/{workspace_id}/novel/export-word.zip")
def export_workspace_novel_word_zip(workspace_id: str, user: CurrentUser = Depends(current_user)) -> Response:
    workspace = thread_store.get_workspace(user.user_id, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    content = thread_store.read_workspace_novel_chapters(user.user_id, workspace_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not content.chapters:
        raise HTTPException(status_code=404, detail="Novel content not found")

    filename_base = _safe_download_name(workspace.title or workspace_id, workspace_id)
    archive = _build_novel_docx_zip(content)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f'{filename_base}-word.zip')}"},
    )


@app.delete("/api/workspaces/{workspace_id}")
async def delete_workspace(workspace_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, str | bool | list[str]]:
    try:
        deleted_thread_ids = thread_store.delete_workspace(user.user_id, workspace_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if deleted_thread_ids is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    # T2.9：清理 checkpoint（分库）+ trace
    for thread_id in deleted_thread_ids:
        agent_service.delete_thread_checkpoint(thread_id, owner_id=user.user_id)
        character_service.delete_thread_checkpoint(thread_id)
    return {"status": "ok", "deleted": workspace_id, "deleted_threads": deleted_thread_ids}


@app.get("/api/threads", response_model=list[ThreadSummary])
def list_threads(workspace_id: str | None = None, user: CurrentUser = Depends(current_user)) -> list[ThreadSummary]:
    return thread_store.list_threads(user.user_id, workspace_id)


@app.post("/api/threads", response_model=ThreadSummary)
def create_thread(payload: ThreadCreateRequest, user: CurrentUser = Depends(current_user)) -> ThreadSummary:
    try:
        return thread_store.create_thread(user.user_id, payload.workspace_id, payload.session_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/threads/{thread_id}", response_model=ThreadSummary)
def update_thread(thread_id: str, payload: ThreadUpdateRequest, user: CurrentUser = Depends(current_user)) -> ThreadSummary:
    try:
        thread = thread_store.update_thread_name(user.user_id, thread_id, payload.session_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread


@app.delete("/api/threads/{thread_id}")
def delete_thread(thread_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, str | bool]:
    thread = thread_store.get_thread(user.user_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    try:
        trace_recorder.delete_thread_runs(thread)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    deleted = thread_store.delete_thread(user.user_id, thread_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found")
    agent_service.delete_thread_checkpoint(thread_id, owner_id=user.user_id)
    character_service.delete_thread_checkpoint(thread_id)
    return {"status": "ok", "deleted": thread_id}


@app.get("/api/threads/{thread_id}/outline", response_model=WorkspaceOutlineContent)
def get_thread_outline(thread_id: str, user: CurrentUser = Depends(current_user)) -> WorkspaceOutlineContent:
    content = thread_store.read_thread_outline(user.user_id, thread_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return content


@app.get("/api/threads/{thread_id}/checkpoint", response_model=CheckpointState)
async def get_thread_checkpoint(thread_id: str, user: CurrentUser = Depends(current_user)) -> CheckpointState:
    thread = thread_store.get_thread(user.user_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return await agent_service.get_thread_checkpoint(thread_id, owner_id=user.user_id)


@app.get("/api/threads/{thread_id}/traces", response_model=list[TraceRunSummary])
def list_thread_traces(thread_id: str, user: CurrentUser = Depends(current_user)) -> list[TraceRunSummary]:
    thread = thread_store.get_thread(user.user_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return trace_recorder.list_runs(thread)


@app.get("/api/threads/{thread_id}/traces/{trace_id}", response_model=TraceDetail)
def get_thread_trace(thread_id: str, trace_id: str, user: CurrentUser = Depends(current_user)) -> TraceDetail:
    thread = thread_store.get_thread(user.user_id, thread_id)
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
def delete_thread_trace(thread_id: str, trace_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, str]:
    thread = thread_store.get_thread(user.user_id, thread_id)
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
async def stream_screenplay(payload: ScreenplayGenerateRequest, user: CurrentUser = Depends(current_user)):
    thread = thread_store.get_thread(user.user_id, payload.thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return StreamingResponse(
        _event_generator(payload, thread, owner_id=user.user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class ImageGenerateRequest(BaseModel):
    """文生图生成请求。"""
    thread_id: str
    prompt: str  # 用户想生成的图片描述
    trace_id: str | None = None
    resume: dict | None = None  # HITL resume（结构化 image_review 反馈，DD4）
    selected_skill_ids: list[str] | None = None  # D9 加载的私有 Skill


async def _image_event_generator(payload: ImageGenerateRequest, thread, *, owner_id: str):
    """文生图 SSE 流（复用 image_agent_service 的 agent + SSE 格式）。

    image agent 走自己的 _build_agent，SSE 事件格式与写作一致（model_stream/
    tool_call/tool_output/interrupt/final），前端按 interrupt 的 kind 路由渲染。
    """
    import asyncio
    from langgraph.types import Command
    model = image_agent_service._resolve_model(owner_id)
    checkpointer = await image_agent_service._resolve_checkpointer(owner_id)
    trace = trace_recorder.create_run(thread, "image.generate.stream")
    yield _sse_event("status", {"status": "started", "trace_id": trace.trace_id})
    trace_queue = trace_recorder.get_active_queue(trace.trace_id)
    if trace_queue is None:
        raise RuntimeError(f"Trace queue not created: {trace.trace_id}")

    agent = image_agent_service._build_agent(
        Path(thread.workspace_path), trace.trace_id, thread.workspace_id, owner_id,
        model=model, checkpointer=checkpointer,
        selected_skill_ids=payload.selected_skill_ids,
    )
    config = {
        "configurable": {"thread_id": thread.thread_id},
        "callbacks": [TraceCallbackHandler(trace_recorder, trace.trace_id)],
        "recursion_limit": 200,
    }
    if payload.resume is not None:
        agent_input = Command(resume=payload.resume)
    else:
        agent_input = {"messages": [{"role": "user", "content": payload.prompt}]}

    heartbeat_task = asyncio.create_task(asyncio.sleep(15))
    agent_events = agent.astream_events(agent_input, config=config, version="v2")
    agent_task = asyncio.create_task(agent_events.__anext__())
    full_result = None
    try:
        while True:
            done, _ = await asyncio.wait(
                {agent_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                heartbeat_task.result()
                yield ": ping\n\n"
                heartbeat_task = asyncio.create_task(asyncio.sleep(15))
            if agent_task in done:
                try:
                    event = agent_task.result()
                except StopAsyncIteration:
                    break
                agent_task = asyncio.create_task(agent_events.__anext__())
                kind = event["event"]
                data = event.get("data", {})
                if kind == "on_chat_model_stream":
                    chunk = data.get("chunk")
                    content = getattr(chunk, "content", "") if chunk else ""
                    if content:
                        yield _sse_event("model_stream", {"content": content})
                elif kind == "on_tool_end":
                    yield _sse_event("tool_output", {
                        "tool": event.get("name", ""),
                        "output": str(data.get("output", ""))[:2000],
                    })
                elif kind == "on_tool_error":
                    yield _sse_event("tool_error", {
                        "tool": event.get("name", ""),
                        "error": str(data.get("error") or data.get("output") or "")[:500],
                    })
                if kind == "on_chain_end" and event.get("name") == "LangGraph":
                    full_result = data.get("output")
        # HITL interrupt 检测（DD4）
        state = await agent.aget_state(config)
        pending = [t for t in state.tasks if t.interrupts]
        if pending:
            iv = pending[0].interrupts[0].value
            payload_dict = iv if isinstance(iv, dict) else {"question": str(iv)}
            # 确保 kind 字段存在（DD4：前端按 kind 路由）
            if "kind" not in payload_dict:
                payload_dict["kind"] = "choice"
            payload_dict["thread_id"] = thread.thread_id
            yield _sse_event("interrupt", payload_dict)
            return
        if full_result is not None:
            content = ""
            if isinstance(full_result, dict):
                for msg in reversed(full_result.get("messages", [])):
                    mc = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
                    if isinstance(mc, str) and mc:
                        content = mc
                        break
            yield _sse_event("final", {"content": content, "thread_id": thread.thread_id})
        trace_recorder.complete_run(thread, trace.trace_id)
    except BaseException as exc:
        trace_recorder.fail_run(thread, trace.trace_id, exc)
        yield _sse_event("error", {"error": str(exc)[:500]})
        raise
    finally:
        heartbeat_task.cancel()
        agent_task.cancel()
        with __import__("contextlib").suppress(BaseException):
            await agent_events.aclose()


@app.post("/api/image/generate/stream")
async def stream_image(payload: ImageGenerateRequest, user: CurrentUser = Depends(current_user)):
    """文生图 SSE 流（Phase 3.9）。

    复用 image_agent_service 的 agent，SSE 事件与写作一致。
    interrupt 事件按 kind 路由：choice=访谈式 / image_review=图像评审。
    """
    thread = thread_store.get_thread(user.user_id, payload.thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return StreamingResponse(
        _image_event_generator(payload, thread, owner_id=user.user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/character/generate", response_model=CharacterGenerateResponse)
def generate_character(payload: CharacterGenerateRequest, user: CurrentUser = Depends(current_user)) -> CharacterGenerateResponse:
    thread = thread_store.get_thread(user.user_id, payload.thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    response = character_service.generate(payload, thread, owner_id=user.user_id)
    thread_store.write_character(user.user_id, thread, response)
    return response
