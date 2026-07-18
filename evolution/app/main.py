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
from app.view.users import router as users_router
from app.versioning.snapshot_api import router as snapshot_router
from app.versioning.elements_api import router as elements_router
from app.view.active import router as active_api_router
from app.view.agent_package import router as agent_package_router
from app.view.sse_stream import router as sse_router
from app.evolve.api import router as evolve_router
from app.tests.api import router as tests_router
from app.eval_agent.api import router as eval_agent_router
from app.view.versions_api import router as versions_router
from app.dataset.api import router as dataset_router
from app.ingestion.scan import start_scan_scheduler
from app.ingestion.user_sync import start_user_sync_scheduler
from app.view.active import start_active_poller
from app.promote.api import router as promote_router
from app.promote.scheduler import start_judge_scheduler
from app.benchmark.api import router as benchmark_router
from app.trace.recorder import EvolutionTraceRecorder


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：建表（幂等）+ 启动兜底扫描 + 启动活跃大盘轮询
    db.init_db()
    start_scan_scheduler()
    start_active_poller()
    # 数据闭环：启动 promote judge 调度器（后台定时扫描生产 trace → judge）
    start_judge_scheduler()
    # 用户映射同步：定时从 executor 拉取用户列表 → user_cache 表（trace 历史列表展示用户名）
    start_user_sync_scheduler()

    # D5/D8：创建 recorder 单例 + 崩溃恢复 + 启动 drain。
    # 顺序保证：init_db 先于 recorder（recorder 写 DB 依赖表已建）。
    recorder = EvolutionTraceRecorder()
    recovered = recorder.recover_pending()
    if recovered:
        import logging
        logging.getLogger("evolution.trace.recorder").info(
            "崩溃恢复：%d 条 running trace 补终态", recovered
        )
    recorder.start_drain()
    app.state.trace_recorder = recorder

    yield

    # 关闭：停 recorder drain + flush 残余事件落盘（D5）。
    await recorder.aclose()


app = FastAPI(title="Writer Evolution", version="0.1.0", lifespan=lifespan)

# D11 CORS：dev 模式前端（localhost:3457）直连 evolution，需允许跨域。
# prod 同源（StaticFiles 托管），CORS 不生效（无跨域）。
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3457", "http://127.0.0.1:3457"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 鉴权中间件（桌面化改造，2026-07-07）：
# 1. NotifyTokenMiddleware：executor → evolution 内网通知（/api/ingestion/*）的 token 校验。
#    最外层（后注册），先于 SSO 处理内网通知路径。
# 2. SSOAuthMiddleware：公网请求（桌面端）的鉴权——回调 executor /api/auth/me 验证
#    session + user_id 白名单。替换旧 InternalKeyMiddleware。
#    SSO 内部对 /api/ingestion/ 前缀放行，交由 NotifyToken 处理，避免双重校验冲突。
from app.core.notify_auth import NotifyTokenMiddleware
from app.core.sso_auth import SSOAuthMiddleware
app.add_middleware(SSOAuthMiddleware)
app.add_middleware(NotifyTokenMiddleware)

# API 路由统一挂 /api 前缀，避免与页面路由（/、/traces、/rules）冲突
app.include_router(ingestion_router, prefix="/api")
app.include_router(traces_router, prefix="/api")
app.include_router(stats_router, prefix="/api")
app.include_router(users_router, prefix="/api")
app.include_router(snapshot_router, prefix="/api")
# Harness 要素展示（前端「Harness 要素」页）
app.include_router(elements_router, prefix="/api")
# 监测前端新增端点（D7 active 富化 / D8 agent-package / D9 SSE stream）
app.include_router(active_api_router, prefix="/api")
app.include_router(agent_package_router, prefix="/api")
app.include_router(sse_router, prefix="/api")
# 进化端单进化 Agent：手动触发 + 查询 + SSE（替换旧 adapt 4 阶段）
app.include_router(evolve_router, prefix="/api")
# 手动单次测试入口（数据集选择 + Agent 版本选择 + 独立测试记录，D-Q9）
app.include_router(tests_router, prefix="/api")
# 评估 Agent（三功能解耦：评估从进化流水线抽离为独立顶层 Agent，S1/S7）
app.include_router(eval_agent_router, prefix="/api")
# 配置版本谱系视图（前端版本谱系页 D8）
app.include_router(versions_router, prefix="/api")
# 数据集管理（数据闭环设计：分层数据集 golden/growing + revision 锁定）
app.include_router(dataset_router, prefix="/api")
# Promote 闸门（数据闭环设计：生产 trace → 数据集的清洗 + 标注流水线）
app.include_router(promote_router, prefix="/api")
# Benchmark 矩阵 + Runner（数据闭环设计：跨版本对比 + golden 升级重跑）
app.include_router(benchmark_router, prefix="/api")
# 大模型 API 配置（桌面化改造，2026-07-07：桌面端唯一 key 配置入口）
from app.config.api import router as config_router
app.include_router(config_router, prefix="/api")
# 管理后台代理（积分制 Phase 4：转发到 executor /api/admin/*）
from app.admin_proxy.router import router as admin_proxy_router
app.include_router(admin_proxy_router, prefix="/api")


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


# 监测前端 web 版（evolution/frontend/）已废弃，只留桌面 App。
# 原 StaticFiles 托管块已移除——桌面 App 走 nginx /evolution-api 反代调 API，
# 不需要容器内托管 web 静态产物。
