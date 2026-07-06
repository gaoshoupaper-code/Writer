"""reflection_library 表的数据访问层（数据闭环设计 D1/A8/D19）。

存储从 badcase trace 自动归纳的"失败模式"。
进化 Agent 启动时按评估发现的问题分类查询相关反思，注入上下文。
借鉴 Reflexion/ExpeL 的"失败→反思→沉淀"模式。

category 来自评估 findings 的 dimension（如 节奏/人物/AI味/套路）。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import app.core.db as db


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── 写 ──────────────────────────────────────────────────────


def add_reflection(
    *,
    category: str,
    pattern: str,
    symptom: str = "",
    suggestion: str = "",
    source_trace_id: str = "",
) -> int:
    """新增一条反思。返回 id。

    不做去重——同一失败模式可能从多个 trace 归纳出来，hit_count 通过
    merge_reflection 合并。简单场景直接 add 即可。
    """
    cur = db.execute(
        """INSERT INTO reflection_library
           (category, pattern, symptom, suggestion, source_traces,
            hit_count, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
        (category, pattern, symptom, suggestion,
         json.dumps([source_trace_id]) if source_trace_id else "[]",
         _now(), _now()),
    )
    return cur.lastrowid  # type: ignore[return-value]


def merge_reflection(
    *,
    category: str,
    pattern: str,
    symptom: str = "",
    suggestion: str = "",
    source_trace_id: str = "",
) -> int:
    """合并反思：同 category + pattern 已存在则累加 source_trace，否则新增。

    返回反思 id。用于同一失败模式被多个 badcase 命中时去重。
    """
    # pattern 做模糊匹配（前 100 字符相似即视为同一条）
    pattern_key = pattern[:100]
    existing = db.query_all(
        """SELECT id, source_traces FROM reflection_library
           WHERE category=? AND pattern LIKE ?""",
        (category, f"{pattern_key}%"),
    )
    if existing:
        row = existing[0]
        # 追加 source_trace
        try:
            traces = json.loads(row["source_traces"]) if row["source_traces"] else []
        except (json.JSONDecodeError, TypeError):
            traces = []
        if source_trace_id and source_trace_id not in traces:
            traces.append(source_trace_id)
        db.execute(
            """UPDATE reflection_library
               SET source_traces=?, symptom=COALESCE(NULLIF(?, ''), symptom),
                   suggestion=COALESCE(NULLIF(?, ''), suggestion),
                   updated_at=?
               WHERE id=?""",
            (json.dumps(traces, ensure_ascii=False), symptom, suggestion, _now(), row["id"]),
        )
        return row["id"]

    return add_reflection(
        category=category, pattern=pattern, symptom=symptom,
        suggestion=suggestion, source_trace_id=source_trace_id,
    )


def increment_hit(reflection_id: int) -> None:
    """被进化引用时 +1（统计反思的利用率）。"""
    db.execute(
        "UPDATE reflection_library SET hit_count=hit_count+1, updated_at=? WHERE id=?",
        (_now(), reflection_id),
    )


# ── 读 ──────────────────────────────────────────────────────


def get(reflection_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM reflection_library WHERE id=?", (reflection_id,))


def list_by_category(category: str, limit: int = 10) -> list[dict[str, Any]]:
    """按分类查反思（进化 Agent 注入用）。按 hit_count 降序（高频优先）。"""
    return db.query_all(
        """SELECT * FROM reflection_library
           WHERE category=? ORDER BY hit_count DESC, updated_at DESC LIMIT ?""",
        (category, limit),
    )


def list_by_categories(categories: list[str], limit_per_category: int = 3) -> list[dict[str, Any]]:
    """按多个分类查反思（评估发现多个问题时用）。

    每个分类取 top N（按 hit_count），合并返回。
    """
    result: list[dict[str, Any]] = []
    for cat in categories:
        result.extend(list_by_category(cat, limit=limit_per_category))
    return result


def list_all(limit: int = 50) -> list[dict[str, Any]]:
    """列全部反思（管理/展示用）。"""
    return db.query_all(
        "SELECT * FROM reflection_library ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )


def count() -> int:
    """反思总数。"""
    row = db.query_one("SELECT COUNT(*) AS n FROM reflection_library")
    return row["n"] if row else 0


__all__ = [
    "add_reflection", "merge_reflection", "increment_hit",
    "get", "list_by_category", "list_by_categories", "list_all", "count",
]
