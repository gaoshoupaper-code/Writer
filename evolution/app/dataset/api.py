"""数据集管理 API（数据闭环设计 A3）。

端点：
  GET  /api/dataset/cases               列出 case（按 layer 过滤，带元数据）
  GET  /api/dataset/golden-revision     当前 golden 锁定的 revision
  POST /api/dataset/cases/{id}/promote  升级 growing→golden（维护者独占，D17）
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.common import evalset
from app.dataset import repo as dataset_repo
from app.dataset import revision

logger = logging.getLogger("evolution.dataset.api")

router = APIRouter(prefix="/dataset", tags=["dataset"])


# ── 列表 ────────────────────────────────────────────────────


@router.get("/cases")
def list_cases(
    layer: str | None = Query(None, description="golden|growing|空=全部"),
) -> dict[str, Any]:
    """列出数据集 case（文件系统 + dataset_meta 元数据合并）。

    以文件系统为准（demand.md 存在才算 case），元数据从 dataset_meta 补充。
    """
    # 文件系统 case 列表（带 title）
    fs_cases = evalset.list_cases_with_title(layer=layer)

    # 元数据索引
    meta_rows = dataset_repo.list_by_layer(layer=layer)
    meta_map = {r["case_id"]: r for r in meta_rows}

    cases = []
    for c in fs_cases:
        case_id = c["case_id"]
        meta = meta_map.get(case_id, {})
        cases.append(
            {
                "case_id": case_id,
                "title": c["title"],
                "layer": c["layer"],
                "source_trace_id": meta.get("source_trace_id"),
                "demand_revision": meta.get("demand_revision"),
                "promoted_at": meta.get("promoted_at"),
                "created_by": meta.get("created_by", "manual"),
                "has_reference": evalset.reference_path(case_id, layer=c["layer"]).exists(),
            }
        )
    return {"cases": cases, "total": len(cases)}


# ── 单 case 内容 ─────────────────────────────────────────────


@router.get("/cases/{case_id}")
def get_case_content(
    case_id: str,
    layer: str | None = Query(None, description="golden|growing|空=自动推导"),
) -> dict[str, Any]:
    """读取单个 case 的 demand.md + reference.md 内容。

    供前端详情侧滑面板展示（列表接口只返回元数据，不返回文件内容）。
    """
    ly = layer or evalset.resolve_layer(case_id) or evalset.DEFAULT_LAYER
    if not evalset.case_exists(case_id, layer=ly):
        raise HTTPException(status_code=404, detail=f"case {case_id} 不存在")

    demand_md = evalset.load_case_demand(case_id, layer=ly)
    title = evalset.parse_title(demand_md, case_id)

    ref_path = evalset.reference_path(case_id, layer=ly)
    reference_md = ref_path.read_text(encoding="utf-8") if ref_path.exists() else None

    meta = dataset_repo.get(case_id) or {}
    return {
        "case_id": case_id,
        "title": title,
        "layer": ly,
        "demand_md": demand_md,
        "reference_md": reference_md,
        "source_trace_id": meta.get("source_trace_id"),
        "demand_revision": meta.get("demand_revision"),
        "promoted_at": meta.get("promoted_at"),
        "created_by": meta.get("created_by", "manual"),
        "status": meta.get("status", "active"),
    }


# ── golden revision ─────────────────────────────────────────


@router.get("/golden-revision")
def get_golden_revision() -> dict[str, Any]:
    """当前 golden 集锁定的 revision + case 列表。

    revision 来自 dataset_meta（锁定值）；若元数据缺失则实时计算（未锁定状态）。
    """
    locked = dataset_repo.get_golden_revision()
    current = revision.compute_golden_revision()
    golden_cases = dataset_repo.get_golden_case_ids()

    return {
        "revision": locked or current,
        "locked": locked is not None,
        "intact": revision.verify_golden_intact(locked) if locked else True,
        "case_count": len(golden_cases),
        "cases": golden_cases,
    }


# ── 升级 growing→golden ────────────────────────────────────


class PromoteRequest(BaseModel):
    """升级请求。maintainer_token 做简单维护者校验（D17）。

    token 在 settings.maintainer_token 配置；空则不校验（开发模式）。
    """
    maintainer_token: str | None = None


@router.post("/cases/{case_id}/promote")
def promote_to_golden(case_id: str, req: PromoteRequest) -> dict[str, Any]:
    """升级 growing→golden（维护者独占，决策 D5/D17）。

    流程：
      1. 校验 case 存在于 growing
      2. 校验 maintainer_token
      3. 物理迁移目录 growing→golden（git mv 风格，保留历史）
      4. 计算新 golden revision（含本次升级后所有 golden case）
      5. 更新 dataset_meta（layer=golden + demand_revision）
    """
    from app.core.settings import settings

    # 维护者校验
    expected = getattr(settings, "maintainer_token", None) or ""
    if expected and req.maintainer_token != expected:
        raise HTTPException(status_code=403, detail="maintainer token 校验失败")

    # 校验 case 在 growing
    if not evalset.case_exists(case_id, layer="growing"):
        raise HTTPException(
            status_code=400,
            detail=f"case {case_id} 不在 growing 层（无法升级）",
        )

    # 物理迁移目录：growing/<case_id> → golden/<case_id>
    src = evalset.layer_root("growing") / case_id
    dst = evalset.layer_root("golden") / case_id
    if dst.exists():
        raise HTTPException(
            status_code=409,
            detail=f"golden 层已存在同名 case: {case_id}",
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    logger.info("case %s 目录迁移: growing → golden", case_id)

    # 计算升级后的 golden revision（所有 golden case 的内容指纹）
    new_revision = revision.lock_golden_revision()

    # 更新 dataset_meta
    existing = dataset_repo.get(case_id)
    if existing:
        dataset_repo.promote_to_golden(case_id, demand_revision=new_revision)
    else:
        # 元数据缺失（理论上不该，但兜底）
        dataset_repo.register_case(
            case_id=case_id,
            layer="golden",
            demand_revision=new_revision,
            created_by="maintainer",
        )

    return {
        "case_id": case_id,
        "layer": "golden",
        "demand_revision": new_revision,
        "golden_case_count": len(dataset_repo.get_golden_case_ids()),
    }


__all__ = ["router"]
