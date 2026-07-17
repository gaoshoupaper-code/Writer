"""One-Hop JOIN 规则（harness 可进化要素）。

论文 §A.1 阶段3：从 anchor 节点扩展一跳邻域，把"排名的节点集"转成
"连通的类型化结构"——暴露 knowledge/location/relationship-delta/reveal/thread 边。

evolution agent 可改本文件来调整扩展策略（如加更多关联类型、调整 cutoff 窗口）。
改完 assemble 注入后立即生效。

签名约束（executor retriever 调用契约）：
  join_rules(store, anchor_type, anchor_row, cutoff) -> list[dict]
  返回的每行 dict 必须含原始 record 字段 + id，retriever 会补 _record_type/_via_join。

与 executor 默认实现的关系：executor retriever 内置 default_join_rules 作为 fallback。
harness 提供本文件时，assemble 注入覆盖默认。
"""
from __future__ import annotations

import json
from typing import Any


def join_rules(
    store: Any,
    anchor_type: str,
    anchor_row: dict,
    cutoff: int,
) -> list[dict]:
    """从 anchor 节点扩展一跳邻域。

    anchor 类型决定扩展哪些 record：
      - character_state → 该角色参与的 scene + relationship_state + object_state
      - scene → 参与该场景的 character_state
      - plot_promise → 暂不深挖（伏笔本身信息已足够）
      - 其他 → 不扩展（叶子节点）

    evolution 可优化：加 plot_promise↔character 通过 thread_id 关联、
    调整扩展深度（论文限定 one-hop，可探索 limited multi-hop）。
    """
    expanded: list[dict] = []
    seen_ids: set[tuple[str, int]] = set()

    def _add(rt: str, rows: list[dict]) -> None:
        for r in rows:
            key = (rt, r["id"])
            if key not in seen_ids:
                r["_record_type"] = rt
                r["_via_join"] = True
                expanded.append(r)
                seen_ids.add(key)

    if anchor_type == "character_state":
        name = anchor_row.get("name", "")
        if name:
            # 该角色参与的场景
            scenes = store.conn.execute(
                "SELECT * FROM scene WHERE source_chapter <= ? "
                "AND (participants LIKE ? OR summary LIKE ?)",
                [cutoff, f'%"{name}"%', f"%{name}%"],
            ).fetchall()
            _add("scene", [dict(r) for r in scenes])

            # 该角色的关系
            rels = store.conn.execute(
                "SELECT * FROM relationship_state WHERE source_chapter <= ? "
                "AND (char_a = ? OR char_b = ?)",
                [cutoff, name, name],
            ).fetchall()
            _add("relationship_state", [dict(r) for r in rels])

            # 该角色持有的物品
            objs = store.conn.execute(
                "SELECT * FROM object_state WHERE source_chapter <= ? AND owner = ?",
                [cutoff, name],
            ).fetchall()
            _add("object_state", [dict(r) for r in objs])

    elif anchor_type == "scene":
        participants_raw = anchor_row.get("participants", "[]")
        try:
            names = json.loads(participants_raw) if isinstance(participants_raw, str) else participants_raw
        except Exception:
            names = []
        for nm in names:
            if nm:
                chars = store.get_current_state("character_state", cutoff=cutoff, entity=nm, limit=1)
                _add("character_state", chars)

    return expanded


__all__ = ["join_rules"]
