"""数据集管理 API（数据闭环设计 A3）。

端点：
  GET  /api/dataset/cases               列出 case（按 layer 过滤，带元数据）
  GET  /api/dataset/cases/{case_id}     单 case 内容（demand.md + reference.md）
  GET  /api/dataset/golden-revision     当前 golden 锁定的 revision

设计决策（重构 2026-07-10）：golden 运行时只读。
  golden 以 git 仓库为权威源（evolution/data/evalset/golden/，随镜像更新），
  运行时禁止写入——变更 golden 只能 git commit + rebuild。
  原运行时 promote（growing→golden）端点已删除（与只读矛盾，曾导致数据丢失）。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

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
    try:
        fs_cases = evalset.list_cases_with_title(layer=layer)
    except Exception:
        logger.error("evalset 文件系统扫描失败", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="数据集目录扫描失败，请检查 evalset 目录状态",
        )

    # 元数据索引（表缺失/查询失败时降级为空，不阻塞文件系统 case 列表）
    try:
        meta_rows = dataset_repo.list_by_layer(layer=layer)
        meta_map = {r["case_id"]: r for r in meta_rows}
    except Exception:
        logger.warning("dataset_meta 查询失败，降级为无元数据", exc_info=True)
        meta_map = {}

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
    # revision 计算与 DB 元数据解耦：compute 为纯文件系统（必成功），
    # DB 查询（锁定值）失败时降级——case 列表始终以文件系统为准（与 list_cases
    # 端点一致），避免手动建目录（未注册元数据）时 case_count 显示 0。
    current = revision.compute_golden_revision()
    golden_cases = evalset.list_cases(layer="golden")
    locked = None
    try:
        locked = dataset_repo.get_golden_revision()
    except Exception:
        logger.warning("dataset_meta 查询失败，golden-revision 降级", exc_info=True)

    return {
        "revision": locked or current,
        "locked": locked is not None,
        "intact": revision.verify_golden_intact(locked) if locked else True,
        "case_count": len(golden_cases),
        "cases": golden_cases,
    }


__all__ = ["router"]
