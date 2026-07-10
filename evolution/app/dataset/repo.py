"""dataset_meta 表的数据访问层（数据闭环设计 A1/A3）。

dataset_meta 存评估集 case 的元数据：layer(golden|growing) / source_trace_id /
demand_revision(git hash) / promoted_at / created_by / status。
demand.md 内容仍是文件真源（evalset.py 读写），本表只存"文件无法表达"的元数据。

关键操作：
- register_case：新 case 入 growing（promote 闸门 accept 时调用）
- get / list / get_golden_revision：查询
- update_demand_revision：golden 内容变更后更新锁定（A4，git 提交后调用）
- archive：软删除

设计决策（重构 2026-07-10）：golden 运行时只读。
  原 promote_to_golden()（运行时升级 growing→golden）已删除——与 golden 只读矛盾。
  golden 变更走 git commit + rebuild，update_demand_revision 用于 git 变更后重锁。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import app.core.db as db


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── 写 ──────────────────────────────────────────────────────


def register_case(
    *,
    case_id: str,
    layer: str = "growing",
    source_trace_id: str | None = None,
    created_by: str = "annotator",
    demand_revision: str | None = None,
) -> None:
    """注册一个新 case 到 dataset_meta（promote 闸门 accept 时调用）。

    幂等：同 case_id 已存在则更新 layer/source_trace/revision（不重复插入）。
    """
    now = _now()
    existing = get(case_id)
    if existing:
        db.execute(
            """UPDATE dataset_meta
               SET layer=?, source_trace_id=?, demand_revision=?, updated_at=?
               WHERE case_id=?""",
            (layer, source_trace_id, demand_revision, now, case_id),
        )
        return
    db.execute(
        """INSERT INTO dataset_meta
           (case_id, layer, source_trace_id, demand_revision, promoted_at,
            created_by, status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
        (case_id, layer, source_trace_id, demand_revision, now, created_by, now),
    )


def update_demand_revision(case_id: str, revision: str) -> None:
    """更新 case 的 demand_revision（golden 内容变更后重新锁定，A4）。

    golden revision 是整个 golden 集的指纹，故一并刷新所有 golden case。
    """
    now = _now()
    db.execute(
        """UPDATE dataset_meta SET demand_revision=?, updated_at=?
           WHERE layer='golden' AND status='active'""",
        (revision, now),
    )


def archive(case_id: str) -> None:
    """归档 case（软删除，status='archived'）。"""
    db.execute(
        "UPDATE dataset_meta SET status='archived', updated_at=? WHERE case_id=?",
        (_now(), case_id),
    )


# ── 读 ──────────────────────────────────────────────────────


def get(case_id: str) -> dict[str, Any] | None:
    """查单个 case 元数据。"""
    return db.query_one(
        "SELECT * FROM dataset_meta WHERE case_id=?",
        (case_id,),
    )


def list_by_layer(layer: str | None = None) -> list[dict[str, Any]]:
    """列出某层（或全部 active）的 case 元数据。"""
    if layer:
        return db.query_all(
            "SELECT * FROM dataset_meta WHERE layer=? AND status='active' ORDER BY case_id",
            (layer,),
        )
    return db.query_all(
        "SELECT * FROM dataset_meta WHERE status='active' ORDER BY layer, case_id"
    )


def get_golden_revision() -> str | None:
    """取当前 golden 集的锁定 revision。

    设计 D4：golden 锁 case 列表 + demand 内容。所有 golden case 应共享同一
    revision（同时锁定）。取任一 golden case 的 demand_revision 作为当前 revision。
    无 golden case 返回 None。
    """
    row = db.query_one(
        "SELECT demand_revision FROM dataset_meta WHERE layer='golden' AND status='active' LIMIT 1"
    )
    return row["demand_revision"] if row else None


def get_golden_case_ids() -> list[str]:
    """当前 golden 集的 case_id 列表（benchmark runner 用）。"""
    rows = db.query_all(
        "SELECT case_id FROM dataset_meta WHERE layer='golden' AND status='active' ORDER BY case_id"
    )
    return [r["case_id"] for r in rows]


__all__ = [
    "register_case",
    "update_demand_revision",
    "archive",
    "get",
    "list_by_layer",
    "get_golden_revision",
    "get_golden_case_ids",
]
