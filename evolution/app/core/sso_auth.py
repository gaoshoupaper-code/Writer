"""SSO 鉴权中间件（桌面化改造，2026-07-07）。

替换旧 InternalKeyMiddleware。evolution 上公网后的唯一鉴权层：
桌面端登录 executor → cookie 传给 evolution → 本中间件回调 executor
/api/auth/me 验证 → 校验 user_id ∈ 白名单 → 放行/403。

双层安全（需求要求"不让用户进进化端"）：
1. 认证：必须持有 executor 有效 session（回调 /api/auth/me 拿 user_id）
2. 授权：user_id 必须在 settings.allowed_user_ids 白名单内

性能优化（决策点 6，选 B）：进程内 TTL 缓存 session→user_id，默认 60s，
避免每个请求都内网往返 executor。monitor 2s 轮询场景下，同 session 60s 内
只回调一次。
"""

from __future__ import annotations

import logging
import time

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.settings import settings

logger = logging.getLogger("evolution.sso_auth")

# 放行路径（无需鉴权）：健康检查、诊断端点、静态根。
_PUBLIC_PATHS = ("/health", "/config")
# 内网通知走单独的 NotifyTokenMiddleware，不在此校验（避免双重鉴权冲突）。
_NOTIFY_PREFIX = "/api/ingestion/"


class SSOAuthMiddleware(BaseHTTPMiddleware):
    """SSO 鉴权：回调 executor 验证 session + user_id 白名单。

    构造参数从 settings 读取，进程内缓存 session→(user_id, expires_ts)。
    """

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._executor_url = settings.executor_url.rstrip("/")
        # 白名单解析为 set（逗号分隔，去空格）
        self._allowed = {
            uid.strip()
            for uid in settings.allowed_user_ids.split(",")
            if uid.strip()
        }
        self._cache_ttl = settings.sso_cache_ttl_seconds
        # session_id → (user_id, is_super_admin, expires_ts)
        self._cache: dict[str, tuple[str, bool, float]] = {}
        # master_key 未配/白名单空时的降级标记（开发模式兼容）
        self._dev_mode = not settings.allowed_user_ids

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path

        # 开发模式（白名单空）：放行全部，等价于旧 internal_api_key 留空
        if self._dev_mode:
            request.state.user_id = "dev"
            request.state.is_super_admin = True
            return await call_next(request)

        # 放行：健康检查、诊断、静态根
        if path == "/" or path.startswith(_PUBLIC_PATHS):
            return await call_next(request)

        # 内网通知路径走 NotifyTokenMiddleware，跳过 SSO
        if path.startswith(_NOTIFY_PREFIX):
            return await call_next(request)

        # 取 session cookie
        session = request.cookies.get("session")
        if not session:
            return _unauthorized("未登录")

        # 查缓存
        user_id, is_super_admin = self._cache_get(session)
        if user_id is None:
            # 缓存 miss/过期 → 回调 executor 验证
            user_id, is_super_admin = await self._verify_with_executor(session)
            if user_id is None:
                return _unauthorized("session 无效或已过期")
            self._cache_set(session, user_id, is_super_admin)

        # 白名单校验
        if user_id not in self._allowed:
            logger.warning("evolution 拒绝非白名单用户访问：user_id=%s path=%s", user_id, path)
            return _forbidden("无权访问进化端")

        # 写入 request.state 供 handler 读取（admin_proxy 用）
        request.state.user_id = user_id
        request.state.is_super_admin = is_super_admin
        return await call_next(request)

    def _cache_get(self, session: str) -> tuple[str, bool] | tuple[None, None]:
        """查缓存，命中且未过期返回 (user_id, is_super_admin)，否则 (None, None)。"""
        cached = self._cache.get(session)
        if cached and cached[2] > time.time():
            return cached[0], cached[1]
        return None, None

    def _cache_set(self, session: str, user_id: str, is_super_admin: bool) -> None:
        """写缓存。无淘汰策略（单人场景缓存条目极少，不必清理）。"""
        self._cache[session] = (user_id, is_super_admin, time.time() + self._cache_ttl)

    async def _verify_with_executor(self, session: str) -> tuple[str, bool] | tuple[None, None]:
        """内网回调 executor /api/auth/me 验证 session，返回 (user_id, is_super_admin) 或 (None, None)。

        executor 的 current_user 依赖会查 sessions 表 + 滚动续期。
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._executor_url}/api/auth/me",
                    cookies={"session": session},
                )
            if resp.status_code != 200:
                return None, None
            data = resp.json()
            user_id = data.get("user_id")
            # executor /api/auth/me 现在返回 is_super_admin（D28），
            # 旧版不返回则默认 False（兼容）
            is_super = bool(data.get("is_super_admin", False))
            return user_id, is_super
        except Exception:
            logger.warning("SSO 回调 executor 失败（executor 不可达？）", exc_info=True)
            return None, None


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": detail})


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": detail})
