"""内网通知鉴权中间件（桌面化改造，2026-07-07）。

executor → evolution 的内网通知（/api/ingestion/notify）专用鉴权。
替换旧 InternalKeyMiddleware 对内网路径的保护。

机制：校验 X-Notify-Token 头 == settings.notify_token。
- token 为空（开发模式）→ 全放行
- token 非空 → 请求必须带 X-Notify-Token 头且匹配

只挂在 /api/ingestion/* 路由（由 SSOAuthMiddleware 放行该前缀，避免双重校验）。
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.settings import settings

logger = logging.getLogger("evolution.notify_auth")

_NOTIFY_PREFIX = "/api/ingestion/"


class NotifyTokenMiddleware(BaseHTTPMiddleware):
    """校验 X-Notify-Token 头。token 配置为空时 no-op（开发模式）。"""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        expected = settings.notify_token
        # 未配置 token → 不校验（开发模式）
        if not expected:
            return await call_next(request)

        path = request.url.path
        # 仅 /api/ingestion/ 下的通知接口受此保护
        if path.startswith(_NOTIFY_PREFIX):
            provided = request.headers.get("x-notify-token", "")
            if provided != expected:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "missing or invalid X-Notify-Token"},
                )

        return await call_next(request)
