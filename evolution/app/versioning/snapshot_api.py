"""snapshot API（Phase 7，取代 surface_api 的查询/发布端点）。

提供整包级（harness_snapshots）的查询 + 发布 API。
surface_api 的 surface 级端点已随 surface_versions 表 DROP 废弃。

端点（/api/snapshots 前缀）：
  GET  /snapshots                 列快照（按版本倒序）
  GET  /snapshots/production      当前 production 快照
  GET  /snapshots/{version}       指定版本快照（元数据，不含 tar）
  GET  /snapshots/{version}/tar   快照 tar 内容（A/B 解压用）
  POST /snapshots/publish         发布当前包为新快照

设计依据：设计文档 D6=① + T5.5（surface_api 语义重定向到 snapshot）。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from app.versioning import snapshot_repo, snapshot_publisher

logger = logging.getLogger("evolution.snapshot_api")

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


@router.get("")
def list_snapshots(status: str | None = None) -> list[dict[str, Any]]:
    """列快照（按版本倒序）。可按 status 过滤。

    返回元数据（不含 tar_blob，避免响应过大）。
    """
    rows = snapshot_repo.list_snapshots(status=status)
    return [{k: v for k, v in r.items() if k != "tar_blob"} for r in rows]


@router.get("/production")
def get_production_snapshot() -> dict[str, Any]:
    """当前 production 快照（元数据）。无则 404。"""
    snap = snapshot_repo.get_production_snapshot()
    if snap is None:
        raise HTTPException(status_code=404, detail="无 production 快照")
    return {k: v for k, v in snap.items() if k != "tar_blob"}


@router.get("/{version}")
def get_snapshot(version: int) -> dict[str, Any]:
    """指定版本快照（元数据）。"""
    snap = snapshot_repo.get_snapshot(version)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"快照 v{version} 不存在")
    return {k: v for k, v in snap.items() if k != "tar_blob"}


@router.get("/{version}/tar")
def get_snapshot_tar(version: int) -> Response:
    """快照 tar 内容（A/B 解压/回放用）。

    返回 application/gzip 二进制。执行端 ab_runner 调此端点拉 tar。
    """
    tar = snapshot_repo.get_snapshot_tar(version)
    if tar is None:
        raise HTTPException(status_code=404, detail=f"快照 v{version} 不存在")
    return Response(content=tar, media_type="application/gzip",
                    headers={"Content-Disposition": f"attachment; filename=harness_v{version}.tar.gz"})


class PublishRequest(BaseModel):
    """发布请求。"""
    change_summary: str | None = None


@router.post("/publish")
def publish_snapshot(req: PublishRequest) -> dict[str, Any]:
    """发布当前 Agent 包为新 production 快照。

    tar 整包目录 → 存快照 → 旧 production 降 retired → 通知执行端。
    """
    result = snapshot_publisher.publish_and_notify(change_summary=req.change_summary)
    if result is None:
        raise HTTPException(status_code=500, detail="发布失败（包目录无 manifest.json？）")
    return result
