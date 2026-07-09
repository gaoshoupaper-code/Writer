"""管理员引导（D6 决策：环境变量引导）。

首启动逻辑：
  若数据库中无任何管理员账号，则：
    1. 按 .env 的 ADMIN_USERNAME / ADMIN_PASSWORD 创建管理员账号。
    2. 生成一个 is_admin_code=1 的永久邀请码（用于未来再注册管理员）。
  幂等：已有管理员时直接返回，不做任何写操作。

调用时机：app lifespan 启动阶段，在 init_database 之后。
"""

from __future__ import annotations

from app.platform.core.db import (
    Database,
    InviteCodeRepository,
    UserRepository,
    get_database,
)
from app.platform.core.settings import get_settings


def bootstrap_admin() -> dict | None:
    """引导管理员。返回创建的管理员信息 dict；已存在管理员时返回 None。

    应在应用启动（lifespan）且数据库初始化后调用。
    """
    settings = get_settings()
    db: Database = get_database()
    users = UserRepository(db)
    invites = InviteCodeRepository(db)

    if users.has_admin():
        return None  # 已有管理员，幂等跳过

    # 创建管理员账号（D28：首个管理员同时置 is_super_admin=1）
    admin = users.create(
        username=settings.admin_username,
        password=settings.admin_password,
        is_admin=True,
        is_super_admin=True,
        workspace_quota=settings.default_workspace_quota,
    )

    # 生成一个永久管理员邀请码（可注册后续管理员）
    admin_invite_codes = invites.create(
        created_by=admin["user_id"], count=1, is_admin_code=True,
    )

    return {
        "user_id": admin["user_id"],
        "username": admin["username"],
        "admin_invite_code": admin_invite_codes[0] if admin_invite_codes else None,
    }
