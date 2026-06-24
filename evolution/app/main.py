"""evolution 服务入口：FastAPI app + lifespan。

第一期路由随各 Phase 逐步挂载：
- Phase 2: ingestion（POST /ingestion/notify）
- Phase 3: traces（GET /traces, /traces/{id}）、stats（/stats/...）
- Phase 4: rules（/rules ...）
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Windows 控制台默认用 GBK（代码页 936）解码，Python 日志输出 UTF-8 中文会乱码。
# 强制 stderr 流用 UTF-8，根治中文日志乱码（root logger + uvicorn logger 都受益）。
try:
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, Exception):
    pass  # 某些环境（重定向到文件）无 reconfigure，忽略

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:     %(message)s",
)

from app.core import db
from app.core.settings import settings
from app.ingestion.ingestion import router as ingestion_router
from app.view.traces import router as traces_router
from app.view.stats import router as stats_router
from app.diagnosis.rules import router as rules_router
from app.improvement.prompts import router as prompts_router
from app.view.evaluation_api import router as evaluation_router
from app.improvement.snapshot_api import router as snapshot_router
from app.view.web.router import router as web_router
from app.ingestion.scan import start_scan_scheduler
from app.view.active import start_active_poller


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：建表（幂等）+ 启动兜底扫描 + 启动活跃大盘轮询
    db.init_db()
    start_scan_scheduler()
    start_active_poller()
    yield
    # 关闭：无特殊清理（SQLite 连接随进程退出释放）


app = FastAPI(title="Writer Evolution", version="0.1.0", lifespan=lifespan)
# API 路由统一挂 /api 前缀，避免与页面路由（/、/traces、/rules）冲突
app.include_router(ingestion_router, prefix="/api")
app.include_router(traces_router, prefix="/api")
app.include_router(stats_router, prefix="/api")
app.include_router(rules_router, prefix="/api")
app.include_router(prompts_router, prefix="/api")
app.include_router(evaluation_router, prefix="/api")
app.include_router(snapshot_router, prefix="/api")
# 页面路由（HTML，无前缀）
app.include_router(web_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "evolution"}


@app.get("/config")
def config() -> dict[str, str]:
    """内部诊断：当前配置（不含敏感信息）。"""
    return {
        "port": str(settings.port),
        "executor_url": str(settings.executor_url),
        "db_path": str(settings.db_path),
    }
