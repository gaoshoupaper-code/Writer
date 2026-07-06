"""入库 growing（数据闭环设计 B5）。

标注 accept 时调用：把 trace 对应的需求 + 编辑终稿写成 growing case。

两种 accept 模式（D6：收/不收 + 归类）：
  1. 归入已有 case：target_case_id 指定，只补充 reference.md（编辑终稿）
  2. 新建 case：标注者提供 demand_md（规范化需求）+ new_case_title

新建 case 时：
  - case_id 自动生成（growing 层下一个可用编号 case-1xx）
  - demand.md 由标注者规范化（前端提供文本）
  - reference.md 存编辑终稿（如有 user_edit 事件）
  - 注册 dataset_meta（layer=growing, source_trace_id）
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.common import evalset
from app.dataset import repo as dataset_repo

logger = logging.getLogger("evolution.promote.promote")

_GROWING_CASE_PREFIX = "case-1"  # growing 层 case 编号 1xx


def _next_growing_case_id() -> str:
    """生成 growing 层下一个可用 case_id（case-101, case-102, ...）。"""
    existing = evalset.list_cases(layer="growing")
    max_num = 100
    for cid in existing:
        # 解析 case-101 → 101
        try:
            num = int(cid.split("-")[-1])
            if num > max_num:
                max_num = num
        except ValueError:
            continue
    return f"{_GROWING_CASE_PREFIX}{max_num + 1 - 100:02d}"


def _build_demand_md(case_id: str, title: str, demand_md: str) -> str:
    """给 demand.md 加 front-matter（title 字段，供 evalset.parse_title 解析）。"""
    now = datetime.now(UTC).isoformat()
    # 如果标注者已提供完整 demand.md（含 front-matter），直接用
    if demand_md.lstrip().startswith("<!--") or demand_md.lstrip().startswith("#"):
        if "<!--" not in demand_md[:10]:
            # 无 front-matter，补上
            header = (
                f"<!--\n元信息（标注入库）：\n"
                f"- title: {title}\n"
                f"- promoted_at: {now}\n"
                f"-->\n\n"
            )
            return header + demand_md
        return demand_md
    # 原始文本，包装成 demand.md
    return (
        f"<!--\n元信息（标注入库）：\n"
        f"- title: {title}\n"
        f"- promoted_at: {now}\n"
        f"-->\n\n# 创作需求\n\n{demand_md}\n"
    )


def promote_to_growing(
    *,
    trace_id: str,
    target_case_id: str | None = None,
    new_case_title: str | None = None,
    demand_md: str | None = None,
    reference_output: str | None = None,
) -> str:
    """执行入库 growing，返回 case_id。

    Args:
        trace_id: 来源 trace（写 dataset_meta.source_trace_id）
        target_case_id: 归入已有 case（模式 1）
        new_case_title: 新建 case 的标题（模式 2）
        demand_md: 新建 case 的需求文本（标注者规范化）
        reference_output: 编辑终稿（D10），存为 reference.md

    Returns:
        case_id（新建或归入的）
    """
    if target_case_id:
        # 模式 1：归入已有 case，只补 reference.md
        return _append_to_existing(target_case_id, trace_id, reference_output)
    else:
        # 模式 2：新建 case
        if not new_case_title:
            raise ValueError("新建 case 需提供 new_case_title")
        return _create_new_case(
            trace_id=trace_id,
            title=new_case_title,
            demand_md=demand_md or "",
            reference_output=reference_output,
        )


def _create_new_case(
    *,
    trace_id: str,
    title: str,
    demand_md: str,
    reference_output: str | None,
) -> str:
    """新建 growing case。"""
    case_id = _next_growing_case_id()
    case_dir = evalset.layer_root("growing") / case_id
    case_dir.mkdir(parents=True, exist_ok=True)

    # demand.md
    full_demand = _build_demand_md(case_id, title, demand_md)
    (case_dir / "demand.md").write_text(full_demand, encoding="utf-8")

    # reference.md（编辑终稿）
    if reference_output:
        evalset.save_reference(case_id, reference_output, layer="growing")

    # dataset_meta
    dataset_repo.register_case(
        case_id=case_id,
        layer="growing",
        source_trace_id=trace_id,
        created_by="annotator",
    )
    logger.info("新建 growing case %s（来自 trace %s）: %s", case_id, trace_id, title)
    return case_id


def _append_to_existing(
    case_id: str,
    trace_id: str,
    reference_output: str | None,
) -> str:
    """归入已有 case，补充 reference.md。"""
    if not evalset.case_exists(case_id, layer="growing"):
        raise FileNotFoundError(f"case {case_id} 不在 growing 层")

    if reference_output:
        # 多次归入的 reference 追加（不覆盖）
        ref_path = evalset.layer_root("growing") / case_id / "reference.md"
        if ref_path.exists():
            existing = ref_path.read_text(encoding="utf-8")
            ref_path.write_text(
                existing + f"\n\n---\n\n<!-- 追加来源 trace: {trace_id} -->\n\n" + reference_output,
                encoding="utf-8",
            )
        else:
            evalset.save_reference(case_id, reference_output, layer="growing")

    # 更新 dataset_meta（可选：记录多个 source_trace）
    existing_meta = dataset_repo.get(case_id)
    if existing_meta and existing_meta.get("source_trace_id"):
        # 已有 source_trace，追加（简单逗号分隔）
        sources = existing_meta["source_trace_id"].split(",")
        if trace_id not in sources:
            sources.append(trace_id)
            import app.core.db as db
            db.execute(
                "UPDATE dataset_meta SET source_trace_id=?, updated_at=? WHERE case_id=?",
                (",".join(sources), datetime.now(UTC).isoformat(), case_id),
            )
    logger.info("归入 growing case %s（补充 trace %s）", case_id, trace_id)
    return case_id


def extract_reference_from_trace(trace_id: str) -> str | None:
    """从 trace 提取编辑终稿（user_edit 事件的编辑后文本，D15）。

    若无 user_edit 事件，返回 None（该 trace 无编辑终稿）。
    TODO(D15/E1)：executor 埋点实现后从 event_payloads 提取。
    """
    import json
    import app.core.db as db

    rows = db.query_all(
        """SELECT payload_json FROM event_payloads
           WHERE trace_id=? AND type='user_edit'
           ORDER BY sequence DESC LIMIT 1""",
        (trace_id,),
    )
    if not rows:
        return None
    try:
        payload = json.loads(rows[0]["payload_json"])
        return payload.get("output", {}).get("edited_text")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None


__all__ = ["promote_to_growing", "extract_reference_from_trace"]
