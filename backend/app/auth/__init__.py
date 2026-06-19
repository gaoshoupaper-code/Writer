"""认证与鉴权（Session Cookie 路线，D4）。

对外契约：
- FastAPI 依赖：current_user / require_admin
- 端点（router）：/api/auth/register, /login, /logout, /me

机制：
- 登录成功发 HttpOnly cookie：session=<token>；Secure; SameSite=Lax。
- 每次请求 current_user 读 cookie → 查 sessions 表 → 滚动续期。
- SSE 链路天然兼容（cookie 自动随 EventSource 携带）。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.platform.core.settings import get_settings
from app.db import (
    InviteCodeRepository,
    SessionRepository,
    UserRepository,
    get_database,
)

SESSION_COOKIE = "session"

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


@dataclass
class CurrentUser:
    """鉴权后注入的当前用户。FastAPI dependency 返回此对象。"""
    user_id: str
    username: str
    is_admin: bool


# ── Schemas ────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    code: str = Field(min_length=1)
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthMeResponse(BaseModel):
    user_id: str
    username: str
    is_admin: bool
    has_api_key: bool


class RegisterResponse(BaseModel):
    user_id: str
    username: str
    is_admin: bool


class LoginResponse(BaseModel):
    user_id: str
    username: str
    is_admin: bool
    has_api_key: bool


# ── Dependencies ───────────────────────────────────────────

def current_user(
    request: Request,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> CurrentUser:
    """所有受保护端点的鉴权依赖。401 若未登录/会话失效。"""
    if not session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    db = get_database()
    sessions = SessionRepository(db)
    row = sessions.get(session)
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "会话不存在")
    if row["expires_at"] < _utcnow_iso():
        sessions.delete(session)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "会话已过期")

    users = UserRepository(db)
    user = users.get_by_id(row["user_id"])
    if user is None or user["disabled"]:
        sessions.delete(session)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不可用")

    # 滚动续期
    settings = get_settings()
    sessions.touch(session, settings.session_ttl_days)

    return CurrentUser(
        user_id=user["user_id"],
        username=user["username"],
        is_admin=bool(user["is_admin"]),
    )


def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    """管理员鉴权依赖。403 若非管理员。"""
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user


# ── 端点 ────────────────────────────────────────────────────

@auth_router.post("/register", response_model=RegisterResponse, status_code=201)
def register(payload: RegisterRequest, response: Response) -> RegisterResponse:
    """邀请码注册：校验码 → 建用户 → 标记码已用 → 发会话 cookie。"""
    db = get_database()
    invites = InviteCodeRepository(db)
    users = UserRepository(db)

    if not invites.is_usable(payload.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "邀请码无效或已被使用")
    invite = invites.get(payload.code)

    if users.get_by_username(payload.username) is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已被占用")

    settings = get_settings()
    is_admin = bool(invite and invite["is_admin_code"])
    quota = settings.default_workspace_quota
    user = users.create(
        username=payload.username,
        password=payload.password,
        is_admin=is_admin,
        workspace_quota=quota,
    )
    invites.mark_used(payload.code, user["user_id"])

    session_id = SessionRepository(db).create(
        user_id=user["user_id"], ttl_days=settings.session_ttl_days,
    )
    _set_session_cookie(response, session_id, settings.session_ttl_days)
    return RegisterResponse(
        user_id=user["user_id"], username=user["username"], is_admin=is_admin,
    )


@auth_router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, response: Response) -> LoginResponse:
    db = get_database()
    users = UserRepository(db)
    user = users.verify(payload.username, payload.password)
    if user is None:
        # 恒定消息，避免用户名枚举
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")

    settings = get_settings()
    session_id = SessionRepository(db).create(
        user_id=user["user_id"], ttl_days=settings.session_ttl_days,
    )
    _set_session_cookie(response, session_id, settings.session_ttl_days)
    return LoginResponse(
        user_id=user["user_id"],
        username=user["username"],
        is_admin=bool(user["is_admin"]),
        has_api_key=users.has_api_key(user["user_id"]),
    )


@auth_router.post("/logout")
def logout(
    response: Response,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict[str, bool]:
    if session:
        SessionRepository(get_database()).delete(session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@auth_router.get("/me", response_model=AuthMeResponse)
def me(user: CurrentUser = Depends(current_user)) -> AuthMeResponse:
    users = UserRepository(get_database())
    return AuthMeResponse(
        user_id=user.user_id,
        username=user.username,
        is_admin=user.is_admin,
        has_api_key=users.has_api_key(user.user_id),
    )


# ── 辅助 ────────────────────────────────────────────────────

def _set_session_cookie(response: Response, session_id: str, ttl_days: int) -> None:
    """设置会话 cookie。Secure 在生产 HTTPS 下生效；本地 HTTP 也允许（samesite lax）。"""
    from datetime import timedelta
    max_age = int(timedelta(days=ttl_days).total_seconds())
    # secure=True 在 HTTP 本地开发会被浏览器丢弃；用环境判断。
    settings = get_settings()
    is_https = settings.writer_frontend_origin.startswith("https://")
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_id,
        max_age=max_age,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/",
    )


def _utcnow_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()
