"""Trace 受治理正文的统一授权与访问审计。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, Request

import app.core.db as db


def require_full_content_access(request: Request) -> None:
    if not getattr(request.state, "is_super_admin", False):
        raise HTTPException(status_code=403, detail="完整 Trace 正文仅超级管理员可访问")


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


__all__ = ["require_full_content_access", "audit_content_access"]
