"""管理后台代理路由实现（AD3/AD7）。

转发到 executor /api/admin/* 和 /api/me/credits，带 SSO session cookie。
executor 侧统一要求 is_super_admin（D28），evolution 侧也做前置校验。

设计：
- 所有 handler 先校验 request.state.is_super_admin（SSO 中间件写入）。
- 用 httpx.AsyncClient 转发，透传 method/body/cookie。
- executor 返回非 2xx 时透传 status_code + body。
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.core.settings import settings

logger = logging.getLogger("evolution.admin_proxy")

router = APIRouter(prefix="/admin", tags=["admin-proxy"])

_EXECUTOR_URL = settings.executor_url.rstrip("/")
_TIMEOUT = 15.0


def _check_super_admin(request: Request) -> JSONResponse | None:
    """前置校验：非超级管理员返回 403。SSO 中间件已写入 request.state。"""
    is_super = getattr(request.state, "is_super_admin", False)
    if not is_super:
        return JSONResponse(status_code=403, content={"detail": "需要超级管理员权限"})
    return None


async def _forward(request: Request, path: str) -> Response:
    """转发请求到 executor，透传 method/body/cookie。"""
    session = request.cookies.get("session", "")
    url = f"{_EXECUTOR_URL}{path}"

    # 读 body（POST/PATCH 有 body，GET 无）
    body = await request.body() if request.method in ("POST", "PATCH", "PUT") else None

    # 透传 headers（只保留 content-type）
    headers = {"Content-Type": "application/json"} if body else {}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers,
                cookies={"session": session},
            )
    except httpx.ConnectError:
        logger.error("executor 不可达：%s", url)
        return JSONResponse(status_code=502, content={"detail": "执行端不可达"})
    except Exception as exc:
        logger.error("转发异常：%s — %s", url, exc)
        return JSONResponse(status_code=502, content={"detail": f"转发异常: {exc}"})

    # 透传 executor 的 response
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


# ════════════════════════════════════════════
#  用户管理
# ════════════════════════════════════════════


@router.get("/users")
async def proxy_list_users(request: Request) -> Response:
    if denied := _check_super_admin(request):
        return denied
    return await _forward(request, "/api/admin/users")


@router.patch("/users/{user_id}")
async def proxy_update_user(request: Request, user_id: str) -> Response:
    if denied := _check_super_admin(request):
        return denied
    return await _forward(request, f"/api/admin/users/{user_id}")


@router.post("/users/{user_id}/reset-password")
async def proxy_reset_password(request: Request, user_id: str) -> Response:
    if denied := _check_super_admin(request):
        return denied
    return await _forward(request, f"/api/admin/users/{user_id}/reset-password")


@router.get("/users/{user_id}/workspaces")
async def proxy_list_user_workspaces(request: Request, user_id: str) -> Response:
    if denied := _check_super_admin(request):
        return denied
    return await _forward(request, f"/api/admin/users/{user_id}/workspaces")


# ════════════════════════════════════════════
#  邀请码管理
# ════════════════════════════════════════════


@router.get("/invite-codes")
async def proxy_list_invite_codes(request: Request) -> Response:
    if denied := _check_super_admin(request):
        return denied
    return await _forward(request, "/api/admin/invite-codes")


@router.post("/invite-codes")
async def proxy_create_invite_codes(request: Request) -> Response:
    if denied := _check_super_admin(request):
        return denied
    return await _forward(request, "/api/admin/invite-codes")


@router.delete("/invite-codes/{code}")
async def proxy_revoke_invite_code(request: Request, code: str) -> Response:
    if denied := _check_super_admin(request):
        return denied
    return await _forward(request, f"/api/admin/invite-codes/{code}")


# ════════════════════════════════════════════
#  积分管理
# ════════════════════════════════════════════


@router.post("/users/{user_id}/credits")
async def proxy_adjust_credits(request: Request, user_id: str) -> Response:
    if denied := _check_super_admin(request):
        return denied
    return await _forward(request, f"/api/admin/users/{user_id}/credits")


@router.get("/users/{user_id}/credits/transactions")
async def proxy_user_credit_transactions(request: Request, user_id: str) -> Response:
    if denied := _check_super_admin(request):
        return denied
    return await _forward(request, f"/api/admin/users/{user_id}/credits/transactions")


@router.get("/credits/transactions")
async def proxy_all_credit_transactions(request: Request) -> Response:
    if denied := _check_super_admin(request):
        return denied
    limit = request.query_params.get("limit", "100")
    return await _forward(request, f"/api/admin/credits/transactions?limit={limit}")


@router.get("/credits/config")
async def proxy_get_credits_config(request: Request) -> Response:
    if denied := _check_super_admin(request):
        return denied
    return await _forward(request, "/api/admin/credits/config")


@router.put("/credits/config/{key}")
async def proxy_update_credits_config(request: Request, key: str) -> Response:
    if denied := _check_super_admin(request):
        return denied
    return await _forward(request, f"/api/admin/credits/config/{key}")


# ════════════════════════════════════════════
#  当前用户余额（非 super_admin 也能查自己）
# ════════════════════════════════════════════


@router.get("/me/credits")
async def proxy_my_credits(request: Request) -> Response:
    """查自己的积分余额（不需要 super_admin）。"""
    return await _forward(request, "/api/me/credits")


@router.get("/me/profile")
async def proxy_my_profile(request: Request) -> Response:
    """查自己的 profile（含 is_super_admin，前端 TopNav 用判断是否显示管理入口）。"""
    return await _forward(request, "/api/auth/me")
