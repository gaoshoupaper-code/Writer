"""管理后台 + 用户账户 API（Phase 4）。

路由分组：
- /api/me：当前用户自己的资料与 API key 管理（任何登录用户）
- /api/admin/*：管理员后台（发码/用户管理/代访问作品只读）

依赖：
- current_user：所有端点需登录
- require_admin：admin 端点需管理员

设计映射：
- D6/D12：管理员发邀请码、独立后台
- D11：/api/me/api-key 供设置页填/清 key
- D14：代访问用户作品只读（可查看不可编辑）
"""

from __future__ import annotations

import secrets as _secrets

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth import CurrentUser, current_user, require_admin
from app.core.settings import get_settings
from app.db import (
    InviteCodeRepository,
    UserRepository,
    WorkspaceRepository,
    get_database,
)

me_router = APIRouter(prefix="/api/me", tags=["me"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


# ════════════════════════════════════════════════════════════
#  /api/me — 当前用户资料与 API key
# ════════════════════════════════════════════════════════════

class MyProfile(BaseModel):
    username: str
    has_api_key: bool
    base_url: str | None
    workspace_count: int
    workspace_quota: int


class ApiKeyRequest(BaseModel):
    api_key: str = Field(min_length=1)
    base_url: str | None = None


@me_router.get("", response_model=MyProfile)
def get_my_profile(user: CurrentUser = Depends(current_user)) -> MyProfile:
    users = UserRepository(get_database())
    row = users.get_by_id(user.user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return MyProfile(
        username=row["username"],
        has_api_key=bool(row["encrypted_api_key"]),
        base_url=row["api_key_base_url"],
        workspace_count=users.workspace_count(user.user_id),
        workspace_quota=row["workspace_quota"],
    )


@me_router.put("/api-key")
def set_my_api_key(payload: ApiKeyRequest, user: CurrentUser = Depends(current_user)) -> dict:
    users = UserRepository(get_database())
    users.set_api_key(user.user_id, payload.api_key, payload.base_url)
    return {"has_api_key": True}


@me_router.delete("/api-key")
def clear_my_api_key(user: CurrentUser = Depends(current_user)) -> dict:
    users = UserRepository(get_database())
    users.clear_api_key(user.user_id)
    return {"has_api_key": False}


# ════════════════════════════════════════════════════════════
#  /api/admin — 邀请码管理
# ════════════════════════════════════════════════════════════

class InviteCodeSummary(BaseModel):
    code: str
    created_at: str
    is_admin_code: bool
    used: bool
    used_by: str | None
    used_at: str | None
    revoked_at: str | None


class CreateInviteCodesRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=50)
    note: str | None = None  # 备注暂不入库，预留


@admin_router.get("/invite-codes", response_model=list[InviteCodeSummary])
def list_invite_codes(admin: CurrentUser = Depends(require_admin)) -> list[InviteCodeSummary]:
    rows = InviteCodeRepository(get_database()).list_all()
    return [
        InviteCodeSummary(
            code=r["code"],
            created_at=r["created_at"],
            is_admin_code=bool(r["is_admin_code"]),
            used=r["used_by"] is not None,
            used_by=r["used_by"],
            used_at=r["used_at"],
            revoked_at=r["revoked_at"],
        )
        for r in rows
    ]


@admin_router.post("/invite-codes", response_model=list[str])
def create_invite_codes(
    payload: CreateInviteCodesRequest,
    admin: CurrentUser = Depends(require_admin),
) -> list[str]:
    return InviteCodeRepository(get_database()).create(
        created_by=admin.user_id, count=payload.count, is_admin_code=False,
    )


@admin_router.delete("/invite-codes/{code}")
def revoke_invite_code(code: str, admin: CurrentUser = Depends(require_admin)) -> dict:
    ok = InviteCodeRepository(get_database()).revoke(code)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邀请码不存在或已吊销")
    return {"status": "ok", "revoked": code}


# ════════════════════════════════════════════════════════════
#  /api/admin — 用户管理
# ════════════════════════════════════════════════════════════

class AdminUserSummary(BaseModel):
    user_id: str
    username: str
    is_admin: bool
    disabled: bool
    has_api_key: bool
    workspace_count: int
    created_at: str


class UpdateUserRequest(BaseModel):
    disabled: bool | None = None
    reset_password: str | None = Field(default=None, min_length=6)


def _random_password() -> str:
    return _secrets.token_urlsafe(12)


@admin_router.get("/users", response_model=list[AdminUserSummary])
def list_users(admin: CurrentUser = Depends(require_admin)) -> list[AdminUserSummary]:
    users = UserRepository(get_database())
    return [
        AdminUserSummary(
            user_id=r["user_id"],
            username=r["username"],
            is_admin=bool(r["is_admin"]),
            disabled=bool(r["disabled"]),
            has_api_key=bool(r["encrypted_api_key"]),
            workspace_count=users.workspace_count(r["user_id"]),
            created_at=r["created_at"],
        )
        for r in users.list_all()
    ]


@admin_router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    admin: CurrentUser = Depends(require_admin),
) -> dict:
    users = UserRepository(get_database())
    target = users.get_by_id(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    result: dict = {"status": "ok"}

    if payload.disabled is not None:
        # 不允许禁用自己（防自锁）和最后一个管理员
        if payload.disabled and user_id == admin.user_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能禁用自己")
        if payload.disabled and target["is_admin"]:
            admins = [u for u in users.list_all() if u["is_admin"] and not u["disabled"]]
            if len(admins) <= 1:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能禁用最后一个管理员")
        users.set_disabled(user_id, payload.disabled)
        result["disabled"] = payload.disabled

    if payload.reset_password is not None:
        users.set_password(user_id, payload.reset_password)
        result["password_reset"] = True

    return result


@admin_router.post("/users/{user_id}/reset-password")
def reset_user_password_auto(
    user_id: str,
    admin: CurrentUser = Depends(require_admin),
) -> dict:
    """生成一个随机临时密码并重置。管理员把临时密码私下发给用户。"""
    users = UserRepository(get_database())
    if users.get_by_id(user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    new_pw = _random_password()
    users.set_password(user_id, new_pw)
    return {"status": "ok", "temp_password": new_pw}


# ════════════════════════════════════════════════════════════
#  /api/admin — 代访问用户作品（D14 只读）
# ════════════════════════════════════════════════════════════

class AdminWorkspaceSummary(BaseModel):
    workspace_id: str
    outline_name: str
    created_at: str
    updated_at: str


@admin_router.get("/users/{user_id}/workspaces", response_model=list[AdminWorkspaceSummary])
def list_user_workspaces(
    user_id: str,
    admin: CurrentUser = Depends(require_admin),
) -> list[AdminWorkspaceSummary]:
    """管理员查看某用户的所有作品（只读列表）。"""
    users = UserRepository(get_database())
    if users.get_by_id(user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    rows = WorkspaceRepository(get_database()).list_by_owner(user_id)
    return [
        AdminWorkspaceSummary(
            workspace_id=r["workspace_id"],
            outline_name=r["outline_name"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


@admin_router.get("/users/{user_id}/workspaces/{workspace_id}/outline")
def read_user_workspace_outline(
    user_id: str,
    workspace_id: str,
    admin: CurrentUser = Depends(require_admin),
) -> dict:
    """管理员代访问：读取某用户作品的 outline（只读）。"""
    from app.core.thread_store import ThreadStore
    from app.db import workspace_dir
    from pathlib import Path

    ws = WorkspaceRepository(get_database()).get(workspace_id, user_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "作品不存在或不属于该用户")
    settings = get_settings()
    ws_path = Path(settings.workspace_root) / user_id / workspace_id
    outline_path = ws_path / "outline.md"
    markdown = ""
    if outline_path.exists():
        try:
            markdown = outline_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            markdown = outline_path.read_text(encoding="gb18030", errors="replace")
    return {"workspace_id": workspace_id, "outline_name": ws["outline_name"], "markdown": markdown}
