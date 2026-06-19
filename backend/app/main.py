"""FastAPI 入口（PR-14 路由收敛后）。

main.py 只保留：app 实例化 + lifespan + CORS + 日志中间件 + 系统级端点（/health、/api/init）。
领域端点已迁到 routers/（screenplay/character/workspaces/threads）+ 各 domain router。
"""
import json
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.admin import admin_router, me_router
from app.auth import CurrentUser, auth_router, current_user
from app.auth.bootstrap import bootstrap_admin
from app.domains.writing.styling.optimizer import StyleOptimizer
from app.domains.writing.styling.router import init_style_module
from app.domains.writing.styling.router import router as style_router
from app.domains.writing.styling.store import CreateTypeStore
from app.domains.writing.expert_agent.services.character import CharacterService
from app.domains.writing.meta import MetaAgentService
from app.platform.agent.middleware import TraceCallbackHandler  # noqa: F401 — image router 用
from app.platform.core.checkpoint_pool import (
    CheckpointPool,
    get_checkpoint_pool,
    init_checkpoint_pool,
)
from app.platform.core.db import Database, UserRepository, get_database, init_database
from app.platform.core.security import load_master_key
from app.platform.core.settings import get_settings
from app.platform.state.thread_store import ThreadStore
from app.platform.trace import TraceRecorder
from app.schemas.screenplay import InitResponse
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont


_active_generations = 0


def _log(event: str, **fields) -> None:
    """诊断日志：结构化 JSON 到 stdout（flush 保证即时输出）。

    为 Phase 1 根因诊断服务——定位 SSE 连接泄漏 / 请求挂起 / 锁竞争。
    设计文档 §5 约定：轻量、零依赖、JSON 到 stdout。
    """
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    print(json.dumps(record, ensure_ascii=False), flush=True)
















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
        init_image_routes(image_agent_service, thread_store, trace_recorder)
    # PR-14：注入 router 共享 context（screenplay/character router 通过 get_*() 访问）
    from app.routers.context import init_router_context
    init_router_context(
        thread_store=thread_store, style_store=style_store, trace_recorder=trace_recorder,
        agent_service=agent_service, character_service=character_service,
        image_agent_service=image_agent_service, style_optimizer=style_optimizer,
    )
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
# PR-14：生成端点 router（从 main.py 抽出，路径前缀 /api）
from app.routers.screenplay import router as screenplay_router
from app.routers.character import router as character_router
app.include_router(screenplay_router, prefix="/api")
app.include_router(character_router, prefix="/api")
# PR-14：workspaces/threads 端点（从 main.py 抽出）
from app.routers.workspaces import router as workspaces_router
from app.routers.threads import router as threads_router
app.include_router(workspaces_router, prefix="/api")
app.include_router(threads_router, prefix="/api")


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


