"""用户缓存查询路由：供 trace 历史列表的用户筛选下拉。

evolution 不维护用户主数据，user_cache 表由 user_sync 定时从 executor 同步。
本路由只提供只读查询，把 user_id→username 映射暴露给前端。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

import app.core.db as db

router = APIRouter(prefix="/users", tags=["users"])


class UserCacheItem(BaseModel):
    """用户缓存项（最小集，够下拉用）。"""
    user_id: str
    username: str


@router.get("/cache", response_model=list[UserCacheItem])
def list_user_cache() -> list[UserCacheItem]:
    """返回 user_cache 全量列表（供前端用户筛选下拉）。

    只返回未禁用用户（disabled=0），按 username 排序。
    """
    rows = db.query_all(
        "SELECT user_id, username FROM user_cache WHERE disabled = 0 ORDER BY username"
    )
    return [UserCacheItem(user_id=r["user_id"], username=r["username"]) for r in rows]
