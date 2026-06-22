"""surface 体系 API 路由（Phase 6 T3.5）。

暴露 surface + manifest 的 REST 端点：
  - /surfaces            surface 版本列表/详情/按 scope 查
  - /manifests           manifest 列表/当前 production/详情
  - /surfaces/{id}/approve  批准 surface 版本并发布 manifest
  - /pipeline/surface/run   手动触发 surface 级流水线一轮

与 harness_api（整体 harness，旧）并存，逐步替代。

设计依据：设计文档 T3.5 接口契约。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.improvement import surface_repo, manifest_repo, manifest_publisher, surface_registry
from app.improvement import pipeline

router = APIRouter(tags=["surface"])


# ── surface 版本 ─────────────────────────────────────────────


@router.get("/surfaces")
def list_surfaces(
    surface_type: str | None = Query(None),
    scope: str | None = Query(None),
    status: str | None = Query(None),
) -> list[dict[str, Any]]:
    """列 surface 版本（可按 type/scope/status 过滤）。

    不带过滤时返回所有 approved 的最新版（manifest 视角）。
    """
    if scope:
        return [dict(r) for r in surface_repo.list_by_scope(scope, status=status)]
    if status:
        return [dict(r) for r in _query_by_status(status, surface_type)]
    # 默认：每条线的 approved 最高版（适合看「当前生效集合」）
    grouped = surface_repo.list_all_approved_grouped()
    rows = []
    for (st, sn, sc), row in sorted(grouped.items()):
        if surface_type and st != surface_type:
            continue
        rows.append(dict(row))
    return rows


def _query_by_status(status: str, surface_type: str | None) -> list[dict[str, Any]]:
    """按 status 查（跨所有 surface 线）。"""
    import app.core.db as db
    if surface_type:
        return db.query_all(
            "SELECT * FROM surface_versions WHERE status=? AND surface_type=? ORDER BY created_at DESC",
            (status, surface_type),
        )
    return db.query_all(
        "SELECT * FROM surface_versions WHERE status=? ORDER BY created_at DESC",
        (status,),
    )


@router.get("/surfaces/types")
def list_surface_types() -> list[dict[str, str]]:
    """列所有合法 surface_type 及其层/content_kind（供前端展示 + 提交校验）。"""
    return [
        {
            "surface_type": st,
            "layer": td.layer.value,
            "content_kind": td.content_kind.value,
            "description": td.description,
        }
        for st, td in sorted(surface_registry.REGISTRY.items())
    ]


@router.get("/surfaces/{surface_type}/{surface_name}/{scope}")
def get_surface_versions(
    surface_type: str, surface_name: str, scope: str,
) -> list[dict[str, Any]]:
    """列某条 surface 线的所有版本（按 version 倒序）。"""
    try:
        surface_registry.get_type_def(surface_type)
    except KeyError as exc:
        raise HTTPException(400, str(exc))
    return [dict(r) for r in surface_repo.list_versions(surface_type, surface_name, scope)]


@router.get("/surfaces/{surface_type}/{surface_name}/{scope}/approved")
def get_approved_surface(
    surface_type: str, surface_name: str, scope: str,
) -> dict[str, Any]:
    """取某条线当前 approved 的最高版本。"""
    ver = surface_repo.get_approved_version(surface_type, surface_name, scope)
    if ver is None:
        raise HTTPException(404, "无 approved 版本")
    return dict(ver)


# ── manifest ─────────────────────────────────────────────────


@router.get("/manifests")
def list_manifests(status: str | None = Query(None)) -> list[dict[str, Any]]:
    """列 manifest（按版本倒序）。"""
    return [dict(r) for r in manifest_repo.list_manifests(status=status)]


@router.get("/manifests/production")
def get_production_manifest() -> dict[str, Any]:
    """查当前 production manifest（含 entries 解析）。"""
    m = manifest_repo.get_production_manifest()
    if m is None:
        raise HTTPException(404, "无 production manifest")
    result = dict(m)
    result["entries"] = manifest_repo.get_entries(m)
    return result


@router.get("/manifests/{manifest_version}")
def get_manifest(manifest_version: int) -> dict[str, Any]:
    """查指定版本 manifest（含 entries 解析）。"""
    m = manifest_repo.get_manifest(manifest_version)
    if m is None:
        raise HTTPException(404, "manifest 不存在")
    result = dict(m)
    result["entries"] = manifest_repo.get_entries(m)
    return result


@router.post("/manifests/publish")
def publish_manifest() -> dict[str, Any]:
    """手动触发 manifest 发布（聚合当前所有 approved surface）。

    自动化场景由 approve_surface 自动触发；此端点供手动重新聚合用。
    """
    result = manifest_publisher.publish_only()
    if result is None:
        raise HTTPException(400, "无 approved surface，无法发布")
    return result


# ── 批准 + 发布 ──────────────────────────────────────────────


@router.post("/surfaces/{surface_version_id}/approve")
def approve_surface(surface_version_id: int) -> dict[str, Any]:
    """批准 surface 版本上线（D17）+ 自动发布 manifest + 通知执行端。

    surface 标 approved → 聚合所有 approved surface → 新 production manifest → 通知。
    """
    try:
        result = pipeline.approve_surface_experiment(surface_version_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if result is None:
        raise HTTPException(404, "surface 版本不存在")
    return result


# ── 流水线触发 ───────────────────────────────────────────────


@router.post("/pipeline/surface/run")
def run_surface_pipeline() -> dict[str, Any]:
    """手动触发一轮 surface 级流水线（Mining → 初筛）。

    A/B 段需单独触发（成本高）。返回本轮处理的签名。
    """
    return pipeline.run_surface_pipeline_cycle()
