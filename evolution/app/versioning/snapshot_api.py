"""snapshot API（去 DB 重构：数据源从 harness_snapshots 表 → registry.json）。

提供整包级的版本查询 API。数据源是 harness 独立仓库内的 registry.json
（版本注册表：版本列表 / 谱系 / production 指针）。

端点（/api/snapshots 前缀）：
  GET  /snapshots                 列版本（按版本倒序，含 status）
  GET  /snapshots/production      当前 production 版本
  GET  /snapshots/{version}       指定版本元数据

版本内容（源码文件）不在本端点返回——通过 /snapshots/{version}/elements 取
（elements_api 从 git 读取真实源文件）。

设计依据：设计文档 20260713_003000（去 DB 轻量化重构）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.versioning import registry_repo

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


@router.get("")
def list_snapshots(status: str | None = None) -> list[dict[str, Any]]:
    """列版本（按版本倒序）。可按 status 过滤（production/retired）。"""
    versions = registry_repo.list_versions()
    if status:
        versions = [v for v in versions if v["status"] == status]
    return versions


@router.get("/production")
def get_production_snapshot() -> dict[str, Any]:
    """当前 production 版本（元数据）。无则 404。"""
    snap = registry_repo.get_production_version()
    if snap is None:
        raise HTTPException(status_code=404, detail="无 production 版本")
    return snap


@router.get("/{version}")
def get_snapshot(version: int) -> dict[str, Any]:
    """指定版本元数据。不存在则 404。"""
    snap = registry_repo.get_version(version)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"版本 v{version} 不存在")
    return snap
