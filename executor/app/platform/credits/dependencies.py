"""积分制 FastAPI 依赖（AD12）。

check_credits_frozen：创作相关 endpoint 的前置拦截。
余额 ≤ 0 的用户被冻结（D16/D26），返回 403 友好提示。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.auth import CurrentUser, current_user


def check_credits_frozen(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    """冻结拦截依赖（D16/D26）。

    余额 ≤ 0 的用户不可触发任何消耗积分的操作（新建/续写/访谈）。
    与 CreditsMiddleware 的兜底拦截形成双保险（AD12）。
    """
    from app.platform.credits.service import get_credits_service

    try:
        svc = get_credits_service()
        if svc.is_frozen(user.user_id):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "积分余额不足，账户已冻结。请联系管理员补充积分。",
            )
    except HTTPException:
        raise
    except Exception:
        # 积分系统不可用时放行（降级：不拦截），中间件层兜底
        pass
    return user
