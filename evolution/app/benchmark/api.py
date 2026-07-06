"""Benchmark API（数据闭环设计 C3）。

端点：
  POST /api/benchmark/run              触发 benchmark（手动，D9）
  POST /api/benchmark/rerun-golden     golden 升级后重跑最近 K=3（D8/D18）
  GET  /api/benchmark/leaderboard      跨版本对比（按 golden_revision）
  GET  /api/benchmark/batches/{id}     查批次状态
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.benchmark import repo, runner

logger = logging.getLogger("evolution.benchmark.api")

router = APIRouter(prefix="/benchmark", tags=["benchmark"])


class RunRequest(BaseModel):
    """触发 benchmark 请求。"""
    version: int | None = None       # None=当前 production
    versions: list[int] | None = None  # 多版本（优先于 version）
    case_ids: list[str] | None = None  # None=golden 全 case


@router.post("/run")
def trigger_run(req: RunRequest) -> dict[str, Any]:
    """手动触发 benchmark（D9）。"""
    versions = req.versions
    if versions is None and req.version is not None:
        versions = [req.version]
    try:
        batch_id = runner.trigger_run(versions=versions, case_ids=req.case_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    batch = repo.get_batch(batch_id)
    return {
        "batch_id": batch_id,
        "status": batch["status"],
        "progress": batch["progress"],
        "golden_revision": batch["golden_revision"],
    }


class RerunGoldenRequest(BaseModel):
    """golden 升级重跑请求。"""
    k: int = 3


@router.post("/rerun-golden")
def rerun_golden(req: RerunGoldenRequest) -> dict[str, Any]:
    """golden 升级后重跑最近 K 个版本（D8/D18/D20）。"""
    try:
        batch_id = runner.trigger_golden_upgrade_rerun(k=req.k)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    batch = repo.get_batch(batch_id)
    return {
        "batch_id": batch_id,
        "status": batch["status"],
        "progress": batch["progress"],
        "golden_revision": batch["golden_revision"],
    }


@router.get("/leaderboard")
def get_leaderboard(
    golden_revision: str | None = Query(None, description="golden revision hash；空=当前锁定值"),
) -> dict[str, Any]:
    """跨版本 leaderboard（按 golden_revision 过滤）。"""
    return repo.get_leaderboard(golden_revision)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, Any]:
    """查批次状态。"""
    batch = repo.get_batch(batch_id)
    if batch["status"] == "not_found":
        raise HTTPException(status_code=404, detail="batch not found")
    return batch


__all__ = ["router"]
