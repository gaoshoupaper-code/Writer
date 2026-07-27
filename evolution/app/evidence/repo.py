"""evidence_packs 表的 CRUD（轨迹证据包，2026-07）。

证据包是评估 Agent 与进化 Agent 的共享事实底座。一条 trace 可有多个版本
（追加，不覆盖旧版）。同 trace 同编译规则版本下，最新的 ready/partial 包
为"当前推荐版本"（is_current=1）。

数据模型见 db.py 的 evidence_packs 表 DDL。
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import app.core.db as db

# 四层结构 + 两个角色视图的 JSON 列。update 时按 key 映射到 _json 列。
_JSON_COLUMNS = {
    "manifest": "manifest_json",
    "facts": "facts_json",
    "semantic": "semantic_json",
    "index": "index_json",
    "eval_view": "eval_view_json",
    "evolve_view": "evolve_view_json",
}

# 终态：编译已结束（成功/降级/失败）。superseded 是被新版本替代的只读终态。
_TERMINAL_STATUSES = {"ready", "partial", "failed", "superseded"}
# 可消费：下游 Agent 可以读的状态。
_CONSUMABLE_STATUSES = {"ready", "partial"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_pack_id() -> str:
    return uuid.uuid4().hex[:12]


def _next_version(trace_id: str) -> int:
    """分配下一个版本号（同 trace 内递增）。"""
    row = db.query_one(
        "SELECT MAX(version) AS max_v FROM evidence_packs WHERE trace_id = ?",
        (trace_id,),
    )
    return (row["max_v"] + 1) if row and row["max_v"] is not None else 1


def create_pack(
    trace_id: str,
    owner_user_id: str,
    *,
    compile_rule_version: str,
    provenance: str = "compile_time_snapshot",
) -> str:
    """新建一个 pending 证据包，返回 pack_id。

    调用方负责在编译成功后调 update_pack 写入四层内容，并调 mark_current
    将本包设为当前推荐版本（同时把旧包标 superseded）。
    """
    pack_id = _new_pack_id()
    now = _now()
    version = _next_version(trace_id)
    db.execute(
        """INSERT INTO evidence_packs
           (pack_id, trace_id, owner_user_id, version, is_current, status,
            provenance, compile_rule_version, llm_calls_used, created_at)
           VALUES (?, ?, ?, ?, 0, 'pending', ?, ?, 0, ?)""",
        (pack_id, trace_id, owner_user_id, version,
         provenance, compile_rule_version, now),
    )
    return pack_id


def update_pack(
    pack_id: str,
    *,
    status: str | None = None,
    manifest: dict[str, Any] | None = None,
    facts: dict[str, Any] | None = None,
    semantic: dict[str, Any] | None = None,
    index: dict[str, Any] | None = None,
    eval_view: dict[str, Any] | None = None,
    evolve_view: dict[str, Any] | None = None,
    failure_reason: str | None = None,
    llm_calls_used: int | None = None,
    finished: bool = False,
) -> None:
    """部分更新证据包字段（只更新非 None 的字段）。

    dict 参数序列化为 JSON 存库（ensure_ascii=False 保中文）。
    finished=True 时写 finished_at（编译结束）。
    """
    sets: list[str] = []
    params: list[Any] = []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if manifest is not None:
        sets.append("manifest_json = ?")
        params.append(json.dumps(manifest, ensure_ascii=False))
    if facts is not None:
        sets.append("facts_json = ?")
        params.append(json.dumps(facts, ensure_ascii=False))
    if semantic is not None:
        sets.append("semantic_json = ?")
        params.append(json.dumps(semantic, ensure_ascii=False))
    if index is not None:
        sets.append("index_json = ?")
        params.append(json.dumps(index, ensure_ascii=False))
    if eval_view is not None:
        sets.append("eval_view_json = ?")
        params.append(json.dumps(eval_view, ensure_ascii=False))
    if evolve_view is not None:
        sets.append("evolve_view_json = ?")
        params.append(json.dumps(evolve_view, ensure_ascii=False))
    if failure_reason is not None:
        sets.append("failure_reason = ?")
        params.append(failure_reason)
    if llm_calls_used is not None:
        sets.append("llm_calls_used = ?")
        params.append(llm_calls_used)
    if finished:
        sets.append("finished_at = ?")
        params.append(_now())

    if not sets:
        return
    params.append(pack_id)
    db.execute(
        f"UPDATE evidence_packs SET {', '.join(sets)} WHERE pack_id = ?",
        tuple(params),
    )


def mark_current(pack_id: str) -> None:
    """将本包设为当前推荐版本，同 trace 其他包的 is_current 清零。

    调用时机：编译成功（ready/partial）后。被取代的旧包标 superseded。
    """
    pack = get_pack(pack_id)
    if pack is None:
        return
    trace_id = pack["trace_id"]
    # 同 trace 其他包：清 is_current，且原 current 包（若有）标 superseded
    db.execute(
        """UPDATE evidence_packs
           SET is_current = 0,
               status = CASE WHEN status IN ('ready','partial') THEN 'superseded' ELSE status END
           WHERE trace_id = ? AND pack_id != ?""",
        (trace_id, pack_id),
    )
    db.execute(
        "UPDATE evidence_packs SET is_current = 1 WHERE pack_id = ?",
        (pack_id,),
    )


def _deserialize(row: dict[str, Any]) -> dict[str, Any]:
    """行反序列化：六个 _json 列解析为 dict（key 去 _json 后缀）。"""
    if not row:
        return row  # type: ignore[return-value]
    for py_key, col in _JSON_COLUMNS.items():
        raw = row.get(col)
        if raw:
            try:
                row[py_key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                row[py_key] = None
        else:
            row[py_key] = None
    return row


def get_pack(pack_id: str) -> dict[str, Any] | None:
    """查单个证据包（含四层 + 两个视图）。"""
    row = db.query_one(
        "SELECT * FROM evidence_packs WHERE pack_id = ?",
        (pack_id,),
    )
    return _deserialize(row) if row else None


def get_current_pack(trace_id: str) -> dict[str, Any] | None:
    """查某 trace 的当前推荐版本（is_current=1）。

    无当前版本时返回 None（调用方决定是否触发编译）。
    """
    row = db.query_one(
        """SELECT * FROM evidence_packs
           WHERE trace_id = ? AND is_current = 1 LIMIT 1""",
        (trace_id,),
    )
    return _deserialize(row) if row else None


def get_consumable_pack(trace_id: str) -> dict[str, Any] | None:
    """查某 trace 最近一条可消费的证据包（ready/partial）。

    供进化闸门强前置校验用。优先取 is_current 的；无则取最新终态可消费的。
    """
    # 先试当前推荐版本
    current = get_current_pack(trace_id)
    if current and current["status"] in _CONSUMABLE_STATUSES:
        return current
    # 退而取最新 ready/partial
    row = db.query_one(
        """SELECT * FROM evidence_packs
           WHERE trace_id = ? AND status IN ('ready','partial')
           ORDER BY version DESC LIMIT 1""",
        (trace_id,),
    )
    return _deserialize(row) if row else None


def list_packs(trace_id: str) -> list[dict[str, Any]]:
    """列某 trace 的所有证据包版本（版本号降序，最新在前）。"""
    rows = db.query_all(
        """SELECT * FROM evidence_packs
           WHERE trace_id = ? ORDER BY version DESC""",
        (trace_id,),
    )
    return [_deserialize(dict(r)) for r in rows]


def get_active_compiling(trace_id: str, compile_rule_version: str) -> dict[str, Any] | None:
    """查某 trace + 编译规则版本下是否有正在编译的包（pending/compiling）。

    供 start 端点做幂等：已有同规则版本在编译则直接返回，避免重复触发。
    """
    row = db.query_one(
        """SELECT * FROM evidence_packs
           WHERE trace_id = ? AND compile_rule_version = ?
             AND status IN ('pending','compiling')
           ORDER BY version DESC LIMIT 1""",
        (trace_id, compile_rule_version),
    )
    return _deserialize(row) if row else None


def get_consumable_by_rule(
    trace_id: str, compile_rule_version: str
) -> dict[str, Any] | None:
    """查某 trace + 编译规则版本下最近一条可消费的包。

    供 start 端点做幂等：已有同规则版本的 ready/partial 则直接返回，不重复编译。
    """
    row = db.query_one(
        """SELECT * FROM evidence_packs
           WHERE trace_id = ? AND compile_rule_version = ?
             AND status IN ('ready','partial')
           ORDER BY version DESC LIMIT 1""",
        (trace_id, compile_rule_version),
    )
    return _deserialize(row) if row else None


def delete_by_trace(trace_id: str) -> int:
    """删除某 trace 的全部证据包（级联删除，trace 删除时调用）。

    返回删除行数。
    """
    cur = db.execute(
        "DELETE FROM evidence_packs WHERE trace_id = ?",
        (trace_id,),
    )
    return cur.rowcount


__all__ = [
    "create_pack",
    "update_pack",
    "mark_current",
    "get_pack",
    "get_current_pack",
    "get_consumable_pack",
    "list_packs",
    "get_active_compiling",
    "get_consumable_by_rule",
    "delete_by_trace",
]
