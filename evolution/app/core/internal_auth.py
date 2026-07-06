"""内网 API Key 中间件（生产加固）。

背景：evolution 是开发者内部工具，本身无业务鉴权；但暴露了
git reset --hard、discard 进化结果等破坏性接口。生产部署虽不挂公网
（仅 docker 内网 expose），仍加一层 X-Internal-Key 校验，防止同机其他
进程/容器误调。

行为：
- settings.internal_api_key 为空 → 全放行（开发模式兼容）。
- 非空 → 所有 /api/ 请求必须带 X-Internal-Key: <key>，否则 401。
- /health、/config、StaticFiles 静态资源始终放行（探活与前端资源无需 key）。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.settings import settings


# 放行路径前缀（无需鉴权）
_PUBLIC_PATHS = ("/health", "/config")


class InternalKeyMiddleware(BaseHTTPMiddleware):
    """校验 X-Internal-Key 头。key 配置为空时此中间件实质 no-op。"""

    async def dispatch(self, request: Request, call_next):
        expected = settings.internal_api_key
        # 未配置 key → 不校验（开发模式）
        if not expected:
            return await call_next(request)

        path = request.url.path

        # 健康检查 / 静态资源（前端产物）放行
        if path == "/" or path.startswith(_PUBLIC_PATHS):
            return await call_next(request)

        # 仅 /api/ 下的接口受保护
        if path.startswith("/api/"):
            provided = request.headers.get("x-internal-key", "")
            if provided != expected:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "missing or invalid X-Internal-Key"},
                )

        return await call_next(request)
