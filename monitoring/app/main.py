"""monitoring 服务入口：FastAPI app + lifespan。

第一期路由随各 Phase 逐步挂载：
- Phase 2: ingestion（POST /ingestion/notify）
- Phase 3: traces（GET /traces, /traces/{id}）、stats（/stats/...）
- Phase 4: rules（/rules ...）
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.settings import settings
from app.ingestion import router as ingestion_router
from app.traces import router as traces_router
from app.stats import router as stats_router
from app.rules import router as rules_router
from app.prompts import router as prompts_router
from app.web.router import router as web_router
from app.scan import start_scan_scheduler
from app.active import start_active_poller


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：建表（幂等）+ 启动兜底扫描 + 启动活跃大盘轮询
    db.init_db()
    start_scan_scheduler()
    start_active_poller()
    yield
    # 关闭：无特殊清理（SQLite 连接随进程退出释放）


app = FastAPI(title="Writer Monitoring", version="0.1.0", lifespan=lifespan)
# API 路由统一挂 /api 前缀，避免与页面路由（/、/traces、/rules）冲突
app.include_router(ingestion_router, prefix="/api")
app.include_router(traces_router, prefix="/api")
app.include_router(stats_router, prefix="/api")
app.include_router(rules_router, prefix="/api")
app.include_router(prompts_router, prefix="/api")
# 页面路由（HTML，无前缀）
app.include_router(web_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "monitoring"}


@app.get("/config")
def config() -> dict[str, str]:
    """内部诊断：当前配置（不含敏感信息）。"""
    return {
        "port": str(settings.port),
        "backend_workspace": str(settings.backend_workspace_path),
        "db_path": str(settings.db_path),
    }
