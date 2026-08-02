"""Trace 受治理正文的统一授权与访问审计。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, Request

import app.core.db as db
from app.core.settings import settings


def require_full_content_access(request: Request) -> None:
    if not getattr(request.state, "is_super_admin", False):
        raise HTTPException(status_code=403, detail="完整 Trace 正文仅超级管理员可访问")


def require_product_owner(request: Request) -> str:
    """守卫：仅产品负责人可操作（REQ-20260802-211032，DEC-005/FR-005）。

    校验 request.state.user_id ∈ settings.product_owner_user_ids（逗号分隔白名单）。
    留空时退化为放行并返回当前 user_id（开发模式兼容，与 allowed_user_ids 的 dev
    降级同构）；生产环境须填配置。返回当前操作者 user_id 供确认审计使用——确认人
    身份从 SSO 写入的 request.state 取，不接受请求体自报。
    """
    user_id = str(getattr(request.state, "user_id", "") or "")
    raw = settings.product_owner_user_ids
    allowed = {uid.strip() for uid in raw.split(",") if uid.strip()}
    if not allowed:
        # dev 降级：白名单空 = 放行（开发模式）。生产环境应配置白名单。
        return user_id or "dev"
    if user_id not in allowed:
        raise HTTPException(status_code=403, detail="仅产品负责人可操作")
    return user_id


def audit_content_access(
    request: Request, action: str, object_type: str, object_id: str
) -> None:
    db.execute(
        """INSERT INTO access_audit
           (actor_user_id, action, object_type, object_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            str(getattr(request.state, "user_id", "unknown")),
            action,
            object_type,
            object_id,
            datetime.now(UTC).isoformat(),
        ),
    )


__all__ = [
    "require_full_content_access",
    "require_product_owner",
    "audit_content_access",
]
