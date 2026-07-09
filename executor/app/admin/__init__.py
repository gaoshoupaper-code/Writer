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

from app.auth import CurrentUser, current_user, require_super_admin
from app.platform.core.settings import get_settings
from app.platform.core.db import (
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
    active_model: str | None
    workspace_count: int
    workspace_quota: int


class ApiKeyRequest(BaseModel):
    api_key: str = Field(min_length=1)
    base_url: str | None = None
    model: str | None = Field(default=None, min_length=1)


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
        active_model=row.get("active_model"),
        workspace_count=users.workspace_count(user.user_id),
        workspace_quota=row["workspace_quota"],
    )


@me_router.put("/api-key")
def set_my_api_key(payload: ApiKeyRequest, user: CurrentUser = Depends(current_user)) -> dict:
    """简单设/改当前 key（兼容旧设置页）。完整配置管理走 /provider-configs。"""
    users = UserRepository(get_database())
    users.set_api_key(user.user_id, payload.api_key, payload.base_url, payload.model)
    return {"has_api_key": True}


# ── Provider 配置历史（多条，可切换）──────────────────────

class ProviderConfigSummary(BaseModel):
    """列表项：不含 key 明文。"""
    config_id: str
    name: str
    base_url: str | None
    model: str
    is_active: bool
    created_at: str
    last_used_at: str | None


class ProviderConfigCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=1)
    base_url: str | None = None
    model: str = Field(min_length=1, max_length=128)
    activate: bool = True


class ProviderConfigUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    api_key: str | None = Field(default=None, min_length=1)
    base_url: str | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)


@me_router.get("/provider-configs", response_model=list[ProviderConfigSummary])
def list_my_configs(user: CurrentUser = Depends(current_user)) -> list[ProviderConfigSummary]:
    from app.platform.core.db import ProviderConfigRepository
    rows = ProviderConfigRepository(get_database()).list_by_owner(user.user_id)
    return [ProviderConfigSummary(**r) for r in rows]


@me_router.post("/provider-configs", response_model=ProviderConfigSummary)
def create_my_config(
    payload: ProviderConfigCreate, user: CurrentUser = Depends(current_user),
) -> ProviderConfigSummary:
    from app.platform.core.db import ProviderConfigRepository
    repo = ProviderConfigRepository(get_database())
    row = repo.create(
        owner_id=user.user_id, name=payload.name, api_key=payload.api_key,
        base_url=payload.base_url, model=payload.model, activate=payload.activate,
    )
    # 去掉 api_key_enc 等内部字段，复用 Summary
    return ProviderConfigSummary(
        config_id=row["config_id"], name=row["name"], base_url=row["base_url"],
        model=row["model"], is_active=bool(row["is_active"]),
        created_at=row["created_at"], last_used_at=row["last_used_at"],
    )


@me_router.put("/provider-configs/{config_id}", response_model=ProviderConfigSummary)
def update_my_config(
    config_id: str, payload: ProviderConfigUpdate,
    user: CurrentUser = Depends(current_user),
) -> ProviderConfigSummary:
    from app.platform.core.db import ProviderConfigRepository
    repo = ProviderConfigRepository(get_database())
    row = repo.update(
        config_id, user.user_id, name=payload.name, api_key=payload.api_key,
        base_url=payload.base_url, model=payload.model,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "配置不存在")
    return ProviderConfigSummary(
        config_id=row["config_id"], name=row["name"], base_url=row["base_url"],
        model=row["model"], is_active=bool(row["is_active"]),
        created_at=row["created_at"], last_used_at=row["last_used_at"],
    )


@me_router.post("/provider-configs/{config_id}/activate")
def activate_my_config(config_id: str, user: CurrentUser = Depends(current_user)) -> dict:
    from app.platform.core.db import ProviderConfigRepository
    ok = ProviderConfigRepository(get_database()).activate(config_id, user.user_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "配置不存在")
    return {"status": "ok", "active": config_id}


@me_router.delete("/provider-configs/{config_id}")
def delete_my_config(config_id: str, user: CurrentUser = Depends(current_user)) -> dict:
    from app.platform.core.db import ProviderConfigRepository
    ok = ProviderConfigRepository(get_database()).delete(config_id, user.user_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "配置不存在")
    return {"status": "ok", "deleted": config_id}


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
    granted_credits: int = 0
    used: bool
    used_by: str | None
    used_at: str | None
    revoked_at: str | None


class CreateInviteCodesRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=50)
    granted_credits: int = Field(default=0, ge=0, description="每个邀请码携带的积分额度（D10）")
    note: str | None = None  # 备注暂不入库，预留


@admin_router.get("/invite-codes", response_model=list[InviteCodeSummary])
def list_invite_codes(admin: CurrentUser = Depends(require_super_admin)) -> list[InviteCodeSummary]:
    rows = InviteCodeRepository(get_database()).list_all()
    return [
        InviteCodeSummary(
            code=r["code"],
            created_at=r["created_at"],
            is_admin_code=bool(r["is_admin_code"]),
            granted_credits=int(r.get("granted_credits", 0)),
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
    admin: CurrentUser = Depends(require_super_admin),
) -> list[str]:
    return InviteCodeRepository(get_database()).create(
        created_by=admin.user_id, count=payload.count,
        is_admin_code=False, granted_credits=payload.granted_credits,
    )


@admin_router.delete("/invite-codes/{code}")
def revoke_invite_code(code: str, admin: CurrentUser = Depends(require_super_admin)) -> dict:
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
    is_super_admin: bool = False
    disabled: bool
    has_api_key: bool
    credits_balance: int = 0
    workspace_count: int
    created_at: str


class UpdateUserRequest(BaseModel):
    disabled: bool | None = None
    reset_password: str | None = Field(default=None, min_length=6)


def _random_password() -> str:
    return _secrets.token_urlsafe(12)


@admin_router.get("/users", response_model=list[AdminUserSummary])
def list_users(admin: CurrentUser = Depends(require_super_admin)) -> list[AdminUserSummary]:
    users = UserRepository(get_database())
    return [
        AdminUserSummary(
            user_id=r["user_id"],
            username=r["username"],
            is_admin=bool(r["is_admin"]),
            is_super_admin=bool(r.get("is_super_admin", 0)),
            disabled=bool(r["disabled"]),
            has_api_key=bool(r["encrypted_api_key"]),
            credits_balance=int(r.get("credits_balance", 0)),
            workspace_count=users.workspace_count(r["user_id"]),
            created_at=r["created_at"],
        )
        for r in users.list_all()
    ]


@admin_router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    admin: CurrentUser = Depends(require_super_admin),
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
    admin: CurrentUser = Depends(require_super_admin),
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
    title: str
    domain: str = "writing"
    created_at: str
    updated_at: str


@admin_router.get("/users/{user_id}/workspaces", response_model=list[AdminWorkspaceSummary])
def list_user_workspaces(
    user_id: str,
    admin: CurrentUser = Depends(require_super_admin),
) -> list[AdminWorkspaceSummary]:
    """管理员查看某用户的所有作品（只读列表）。"""
    users = UserRepository(get_database())
    if users.get_by_id(user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    rows = WorkspaceRepository(get_database()).list_by_owner(user_id)
    return [
        AdminWorkspaceSummary(
            workspace_id=r["workspace_id"],
            title=r.get("title", r.get("outline_name", "")),
            domain=r.get("domain", "writing"),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


@admin_router.get("/users/{user_id}/workspaces/{workspace_id}/outline")
def read_user_workspace_outline(
    user_id: str,
    workspace_id: str,
    admin: CurrentUser = Depends(require_super_admin),
) -> dict:
    """管理员代访问：读取某用户作品的 outline（只读）。"""
    from app.platform.state.thread_store import ThreadStore
    from app.platform.core.db import workspace_dir
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
    return {"workspace_id": workspace_id, "title": ws.get("title", ws.get("outline_name", "")), "markdown": markdown}


# ════════════════════════════════════════════════════════════
#  /api/admin — 积分管理（D8/D18/D19）
# ════════════════════════════════════════════════════════════


class AdjustCreditsRequest(BaseModel):
    amount: int = Field(description="调整额度（正=充值，负=扣减）")
    note: str = Field(default="", max_length=200)


@admin_router.post("/users/{user_id}/credits")
def adjust_user_credits(
    user_id: str,
    payload: AdjustCreditsRequest,
    admin: CurrentUser = Depends(require_super_admin),
) -> dict:
    """管理员手动调整用户积分（D8）。"""
    users = UserRepository(get_database())
    if users.get_by_id(user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    from app.platform.credits.service import get_credits_service
    balance = get_credits_service().admin_adjust(
        user_id=user_id, amount=payload.amount,
        note=payload.note or "管理员调整", admin_id=admin.user_id,
    )
    return {"status": "ok", "balance": balance}


@admin_router.get("/users/{user_id}/credits/transactions")
def list_user_credit_transactions(
    user_id: str,
    limit: int = 50,
    admin: CurrentUser = Depends(require_super_admin),
) -> list[dict]:
    """查某用户积分流水（D18-F/G）。"""
    users = UserRepository(get_database())
    if users.get_by_id(user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    from app.platform.credits.service import get_credits_service
    return get_credits_service().list_user_transactions(user_id, limit)


@admin_router.get("/credits/transactions")
def list_all_credit_transactions(
    limit: int = 100,
    admin: CurrentUser = Depends(require_super_admin),
) -> list[dict]:
    """全局积分流水（D18-G）。"""
    from app.platform.credits.service import get_credits_service
    return get_credits_service().list_all_transactions(limit)


# ════════════════════════════════════════════════════════════
#  /api/me/credits — 写作端查积分余额（D11）
# ════════════════════════════════════════════════════════════


@me_router.get("/credits")
def get_my_credits(user: CurrentUser = Depends(current_user)) -> dict:
    """当前用户的积分余额（D11 写作端展示用）。"""
    try:
        from app.platform.credits.service import get_credits_service
        balance = get_credits_service().get_balance(user.user_id)
    except Exception:
        balance = 0
    return {"balance": balance}


# ════════════════════════════════════════════════════════════
#  /api/admin — 积分暗调参数管理（AD11）
# ════════════════════════════════════════════════════════════


@admin_router.get("/credits/config")
def get_credits_config(admin: CurrentUser = Depends(require_super_admin)) -> dict:
    """获取积分暗调参数（管理页展示）。"""
    from app.platform.credits.service import get_credits_service
    svc = get_credits_service()
    return svc._config.get_all_for_display()


class UpdateConfigRequest(BaseModel):
    value: str = Field(min_length=1)


@admin_router.put("/credits/config/{key}")
def update_credits_config(
    key: str,
    payload: UpdateConfigRequest,
    admin: CurrentUser = Depends(require_super_admin),
) -> dict:
    """修改积分暗调参数（AD11：在线改，不重启，自动刷新缓存）。"""
    from app.platform.credits.service import get_credits_service
    svc = get_credits_service()
    svc._config.set(key, payload.value)
    return {"status": "ok", "key": key, "value": payload.value}
