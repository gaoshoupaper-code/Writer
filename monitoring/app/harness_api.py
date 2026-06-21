"""Self-Harness API 路由（Phase 5 T5.1）。

暴露进化流水线的 REST 端点：
  - /signatures          失败签名列表/详情
  - /harnesses           harness 版本列表/diff
  - /experiments         A/B 实验列表/批准/拒绝
  - /pipeline/run        手动触发流水线一轮

设计依据：设计文档 T5.1 接口契约 + pipeline/mining/harness_repo。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import app.db as db
from app import mining, harness_repo, pipeline, calibrate
from app.settings import settings

router = APIRouter(tags=["self-harness"])


# ── 失败签名 ────────────────────────────────────────────────


@router.get("/signatures")
def list_signatures(status: str | None = Query(None)) -> list[dict[str, Any]]:
    """列失败签名（可按 status 过滤：open/mining/proposed/resolved）。"""
    return mining.list_signatures(status)


@router.get("/signatures/{signature_id}")
def get_signature(signature_id: int) -> dict[str, Any]:
    """查单个失败签名详情（含关联的 badcase）。"""
    sig = db.query_one("SELECT * FROM failure_signatures WHERE id=?", (signature_id,))
    if sig is None:
        raise HTTPException(404, "签名不存在")
    result = dict(sig)
    result["badcases"] = mining.list_badcases(signature_id=signature_id)
    return result


@router.post("/signatures/mine")
def trigger_mining(background: bool = True) -> dict[str, Any]:
    """手动触发 Mining（检查攒够的维度，提炼签名）。"""
    signatures = mining.check_and_mine_all()
    return {"mined": len(signatures), "signatures": signatures}


# ── harness 版本 ────────────────────────────────────────────


@router.get("/harnesses")
def list_harnesses(status: str | None = Query(None)) -> list[dict[str, Any]]:
    """列 harness 版本（可按 status 过滤）。"""
    return [dict(r) for r in harness_repo.list_versions(status)]


@router.get("/harnesses/production")
def get_production() -> dict[str, Any]:
    """查当前 production harness 版本。"""
    prod = harness_repo.get_production_version()
    if prod is None:
        raise HTTPException(404, "无 production harness")
    return dict(prod)


@router.get("/harnesses/{version}/diff")
def get_harness_diff(version: int, against: int | None = None) -> dict[str, Any]:
    """查某版本的 harness 代码 diff（against 不传则对比 production）。

    diff 从 harness_versions.code_path（版本记录的绝对路径）读代码，不依赖 harnesses_root。
    """
    target = harness_repo.get_version(version)
    if target is None:
        raise HTTPException(404, "版本不存在")
    if against is None:
        prod = harness_repo.get_production_version()
        if prod is None:
            raise HTTPException(400, "无 production 版本可对比")
        against = prod["version"]
    try:
        # get_harness_diff 需要 harnesses_root 参数但实际从 code_path 读，传占位
        from pathlib import Path
        diff = harness_repo.get_harness_diff(against, version, Path("."))
    except Exception as exc:
        raise HTTPException(500, f"生成 diff 失败: {exc}")
    return {"version": version, "against": against, "diff": diff}


# ── A/B 实验 ────────────────────────────────────────────────


@router.get("/experiments")
def list_experiments(status: str | None = Query(None)) -> list[dict[str, Any]]:
    """列 A/B 实验。"""
    if status:
        rows = db.query_all(
            "SELECT * FROM harness_experiments WHERE status=? ORDER BY id DESC", (status,)
        )
    else:
        rows = db.query_all("SELECT * FROM harness_experiments ORDER BY id DESC")
    return [dict(r) for r in rows]


class ApproveBody(BaseModel):
    pass


@router.post("/experiments/{experiment_id}/approve")
def approve(experiment_id: int) -> dict[str, Any]:
    """人工批准 A/B 实验胜出的候选上线（D17）。

    仅 verdict=win 可批准。批准后 candidate 升 production label，原 production 降级。
    """
    try:
        return pipeline.approve_experiment(experiment_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/experiments/{experiment_id}/reject")
def reject(experiment_id: int) -> dict[str, Any]:
    """拒绝候选（签名回 open，等更多数据或换策略）。"""
    result = pipeline.reject_experiment(experiment_id)
    if result is None:
        raise HTTPException(404, "实验不存在")
    return result


# ── 流水线触发 ──────────────────────────────────────────────


@router.post("/pipeline/run")
def run_pipeline() -> dict[str, Any]:
    """手动触发一轮流水线（Mining → 初筛）。

    A/B 段需单独触发（成本高）。返回本轮处理的签名。
    """
    harnesses_root = getattr(settings, "harnesses_root", "harnesses")
    return pipeline.run_pipeline_cycle(harnesses_root)


# ── 校准 ───────────────────────────────────────────────────


@router.get("/calibration")
def list_calibration() -> list[dict[str, Any]]:
    """查 judge 方差校准结果（定 seed 数 N 的依据）。"""
    return calibrate.list_calibrations()


@router.get("/calibration/recommended-n")
def get_recommended_n() -> dict[str, Any]:
    """查当前推荐的 A/B seed 数 N（取所有校准维度最大值）。"""
    return {"recommended_n": calibrate.get_max_n_for_experiment()}
