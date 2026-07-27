"""evidence_dossiers 表的 CRUD（证据卷宗，2026-07）。

证据卷宗是评估 Agent 与进化 Agent 的共享事实底座。一条 trace 可有多个版本
（追加，不覆盖旧版）。同 trace 同编译规则版本下，最新的 ready/partial 卷宗
为"当前推荐版本"（is_current=1）。

数据模型见 db.py 的 evidence_dossiers 表 DDL。

术语映射（2026-07-27 重命名）：
- DB 表 evidence_packs → evidence_dossiers（表名已改）。
- DB 列名仍是 pack_id（SQLite RENAME 不改列名，重建表风险大）。
- 本 repo 对外统一用 dossier_id：_deserialize 把 pack_id 映射成 dossier_id，
  函数签名用 dossier_id，内部 SQL 仍按 pack_id 列名读写。上层代码不感知 pack_id。
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


class DossierImmutableError(Exception):
    """终态证据卷宗不可原地修改（需求 §35 不可变性，B4）。

    重新编纂或纠错必须走 create_dossier 产生新版本。
    """



def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_dossier_id() -> str:
    return uuid.uuid4().hex[:12]


def _next_version(trace_id: str) -> int:
    """分配下一个版本号（同 trace 内递增）。"""
    row = db.query_one(
        "SELECT MAX(version) AS max_v FROM evidence_dossiers WHERE trace_id = ?",
        (trace_id,),
    )
    return (row["max_v"] + 1) if row and row["max_v"] is not None else 1


def create_dossier(
    trace_id: str,
    owner_user_id: str,
    *,
    compile_rule_version: str,
    provenance: str = "compile_time_snapshot",
) -> str:
    """新建一个 pending 证据卷宗，返回 dossier_id。

    调用方负责在编译成功后调 update_dossier 写入四层内容，并调 mark_current
    将本卷宗设为当前推荐版本（同时把旧卷宗标 superseded）。
    """
    dossier_id = _new_dossier_id()
    now = _now()
    version = _next_version(trace_id)
    db.execute(
        """INSERT INTO evidence_dossiers
           (pack_id, trace_id, owner_user_id, version, is_current, status,
            provenance, compile_rule_version, llm_calls_used, created_at)
           VALUES (?, ?, ?, ?, 0, 'pending', ?, ?, 0, ?)""",
        (dossier_id, trace_id, owner_user_id, version,
         provenance, compile_rule_version, now),
    )
    return dossier_id


def update_dossier(
    dossier_id: str,
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
    """部分更新证据卷宗字段（只更新非 None 的字段）。

    dict 参数序列化为 JSON 存库（ensure_ascii=False 保中文）。
    finished=True 时写 finished_at（编译结束）。

    不可变性保护（B4，需求 §35）：终态卷宗（ready/partial/failed/superseded）
    一经落库不可原地修改。重新编纂或纠错必须走 create_dossier 产生新版本。
    编译流程内的 pending→compiling→终态 是单次流程，不会重复 update 已终态卷宗。
    """
    # 不可变性 guard：查当前状态，已是终态则拒绝
    existing = db.query_one(
        "SELECT status FROM evidence_dossiers WHERE pack_id = ?",
        (dossier_id,),
    )
    if existing is None:
        raise DossierImmutableError(f"证据卷宗 {dossier_id} 不存在")
    if existing["status"] in _TERMINAL_STATUSES:
        raise DossierImmutableError(
            f"证据卷宗 {dossier_id} 已是终态（{existing['status']}），不可原地修改。"
            f"重新编纂或纠错须创建新版本。"
        )

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
    params.append(dossier_id)
    db.execute(
        f"UPDATE evidence_dossiers SET {', '.join(sets)} WHERE pack_id = ?",
        tuple(params),
    )


def mark_current(dossier_id: str) -> None:
    """将本卷宗设为当前推荐版本，同 trace 其他卷宗的 is_current 清零。

    调用时机：编译成功（ready/partial）后。被取代的旧卷宗标 superseded。
    """
    dossier = get_dossier(dossier_id)
    if dossier is None:
        return
    trace_id = dossier["trace_id"]
    # 同 trace 其他卷宗：清 is_current，且原 current 卷宗（若有）标 superseded
    db.execute(
        """UPDATE evidence_dossiers
           SET is_current = 0,
               status = CASE WHEN status IN ('ready','partial') THEN 'superseded' ELSE status END
           WHERE trace_id = ? AND pack_id != ?""",
        (trace_id, dossier_id),
    )
    db.execute(
        "UPDATE evidence_dossiers SET is_current = 1 WHERE pack_id = ?",
        (dossier_id,),
    )


def _deserialize(row: dict[str, Any]) -> dict[str, Any]:
    """行反序列化：六个 _json 列解析为 dict（key 去 _json 后缀）；pack_id 映射为 dossier_id。"""
    if not row:
        return row  # type: ignore[return-value]
    # DB 列名 pack_id → 对外 dossier_id（术语统一，上层不感知底层列名）
    if "pack_id" in row:
        row["dossier_id"] = row["pack_id"]
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


def get_dossier(dossier_id: str) -> dict[str, Any] | None:
    """查单个证据卷宗（含四层 + 两个视图）。"""
    row = db.query_one(
        "SELECT * FROM evidence_dossiers WHERE pack_id = ?",
        (dossier_id,),
    )
    return _deserialize(row) if row else None


def get_current_dossier(trace_id: str) -> dict[str, Any] | None:
    """查某 trace 的当前推荐版本（is_current=1）。

    无当前版本时返回 None（调用方决定是否触发编译）。
    """
    row = db.query_one(
        """SELECT * FROM evidence_dossiers
           WHERE trace_id = ? AND is_current = 1 LIMIT 1""",
        (trace_id,),
    )
    return _deserialize(row) if row else None


def get_consumable_dossier(trace_id: str) -> dict[str, Any] | None:
    """查某 trace 最近一条可消费的证据卷宗（ready/partial）。

    供进化闸门强前置校验用。优先取 is_current 的；无则取最新终态可消费的。
    """
    # 先试当前推荐版本
    current = get_current_dossier(trace_id)
    if current and current["status"] in _CONSUMABLE_STATUSES:
        return current
    # 退而取最新 ready/partial
    row = db.query_one(
        """SELECT * FROM evidence_dossiers
           WHERE trace_id = ? AND status IN ('ready','partial')
           ORDER BY version DESC LIMIT 1""",
        (trace_id,),
    )
    return _deserialize(row) if row else None


def list_dossiers(trace_id: str) -> list[dict[str, Any]]:
    """列某 trace 的所有证据卷宗版本（版本号降序，最新在前）。"""
    rows = db.query_all(
        """SELECT * FROM evidence_dossiers
           WHERE trace_id = ? ORDER BY version DESC""",
        (trace_id,),
    )
    return [_deserialize(dict(r)) for r in rows]


def get_active_compiling(trace_id: str, compile_rule_version: str) -> dict[str, Any] | None:
    """查某 trace + 编译规则版本下是否有正在编译的卷宗（pending/compiling）。

    供 start 端点做幂等：已有同规则版本在编译则直接返回，避免重复触发。
    """
    row = db.query_one(
        """SELECT * FROM evidence_dossiers
           WHERE trace_id = ? AND compile_rule_version = ?
             AND status IN ('pending','compiling')
           ORDER BY version DESC LIMIT 1""",
        (trace_id, compile_rule_version),
    )
    return _deserialize(row) if row else None


def get_consumable_by_rule(
    trace_id: str, compile_rule_version: str
) -> dict[str, Any] | None:
    """查某 trace + 编译规则版本下最近一条可消费的卷宗。

    供 start 端点做幂等：已有同规则版本的 ready/partial 则直接返回，不重复编译。
    """
    row = db.query_one(
        """SELECT * FROM evidence_dossiers
           WHERE trace_id = ? AND compile_rule_version = ?
             AND status IN ('ready','partial')
           ORDER BY version DESC LIMIT 1""",
        (trace_id, compile_rule_version),
    )
    return _deserialize(row) if row else None


def delete_by_trace(trace_id: str) -> int:
    """删除某 trace 的全部证据卷宗（级联删除，trace 删除时调用）。

    返回删除行数。
    """
    cur = db.execute(
        "DELETE FROM evidence_dossiers WHERE trace_id = ?",
        (trace_id,),
    )
    return cur.rowcount


__all__ = [
    "DossierImmutableError",
    "create_dossier",
    "update_dossier",
    "mark_current",
    "get_dossier",
    "get_current_dossier",
    "get_consumable_dossier",
    "list_dossiers",
    "get_active_compiling",
    "get_consumable_by_rule",
    "delete_by_trace",
]
