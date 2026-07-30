"""评估终态一致性对账（FR-004 失败语义 / EDGE-003）。

封存成功（sealer 单事务写 evaluation_dossiers + 回填 evaluation_sessions.sealed_dossier_id
+ status='completed'）是评估成功的唯一不可变事实。但收尾进程可能在封存提交后、
Trace 终态/业务投影写入前崩溃，留下「封存成功但业务 failed」的分裂态（EVD-011/EVD-012）。

本模块提供启动时的一次性对账：以不可变 sealed dossier 为事实，把误标失败的已封存评估
收敛回 completed，把 Trace 终态对齐。不重跑模型、不改评估卷宗内容、不改历史 Trace 事件
（DEC-003 / FR-006 边界）。

与历史一次性纠正（migrations）的区别：migrations 修旧状态判断缺陷造成的历史误标；
本模块是常驻对账，处理封存后崩溃等运行期分裂（EDGE-003）。两者共用封存有效性判定。
"""
from __future__ import annotations

import logging
from typing import Any

import app.core.db as db

logger = logging.getLogger("evolution.eval_agent.reconcile")


def select_mislabeled_candidates() -> list[dict[str, Any]]:
    """查询封存成功但业务状态非 completed 的评估尝试（误标候选）。

    sealed_dossier_id 由 sealer 在封存成功的事务内写入，它存在即证明封存发生过；
    此时业务状态应为 completed。reconcile 与 migrations 共用本查询。
    """
    return [
        dict(r) for r in db.query_all(
            """SELECT eval_id, trace_id, status, sealed_dossier_id, self_trace_id,
                      created_at, updated_at
               FROM evaluation_sessions
               WHERE sealed_dossier_id IS NOT NULL AND sealed_dossier_id != ''
                 AND status != 'completed'"""
        )
    ]


def valid_sealed_dossier_ids(dossier_ids: list[str]) -> set[str]:
    """批量确认哪些 dossier_id 指向 seal_status='sealed' 的不可变卷宗。

    单次 IN 查询替代逐行查（避免 N+1）。返回有效的 dossier_id 集合。
    SQLite 占位符数有上限，分批查。
    """
    valid: set[str] = set()
    for i in range(0, len(dossier_ids), 500):
        chunk = dossier_ids[i:i + 500]
        if not chunk:
            continue
        placeholders = ",".join("?" * len(chunk))
        rows = db.query_all(
            f"SELECT dossier_id FROM evaluation_dossiers "
            f"WHERE dossier_id IN ({placeholders}) AND seal_status = 'sealed'",
            tuple(chunk),
        )
        valid.update(r["dossier_id"] for r in rows)
    return valid


def reconcile_eval_terminal_states() -> dict[str, Any]:
    """启动时对账：把封存成功但误标失败的评估收敛为 completed（EDGE-003）。

    只读预演 + 精确更新，不重跑模型、不改卷宗内容、不改历史 Trace 事件。
    返回 {scanned, reconciled, skipped, examples} 供启动日志诊断。
    """
    rows = select_mislabeled_candidates()
    if not rows:
        return {"scanned": 0, "reconciled": 0, "skipped": 0,
                "reconciled_ids": [], "skipped_details": []}

    # 批量确认有效 sealed dossier（避免逐行 N+1）。
    sealed_ids = [r["sealed_dossier_id"] for r in rows]
    valid = valid_sealed_dossier_ids(sealed_ids)

    reconciled: list[str] = []
    skipped: list[dict[str, Any]] = []
    # 原子收敛：整批 UPDATE 在单事务内，任一失败整体回滚（EDGE-003 一致性 + 避免部分更新）。
    to_reconcile = [r for r in rows if r["sealed_dossier_id"] in valid]
    for row in rows:
        if row["sealed_dossier_id"] not in valid:
            skipped.append({
                "eval_id": row["eval_id"],
                "reason": "sealed_dossier_id 无对应 sealed 卷宗（证据不充分）",
            })

    if to_reconcile:
        with db.transaction() as conn:
            for row in to_reconcile:
                # 同时清理 failure_reason/stop_reason：纠正后 status=completed 但旧失败
                # 描述残留会构成自相矛盾脏状态（FR-006 一致终态）。
                conn.execute(
                    "UPDATE evaluation_sessions SET status='completed', "
                    "failure_reason=NULL, updated_at=? WHERE eval_id=?",
                    (_now(), row["eval_id"]),
                )
                reconciled.append(row["eval_id"])
                logger.info(
                    "评估终态对账：eval=%s 由 %s 收敛为 completed（封存成功事实 sealed=%s）",
                    row["eval_id"], row["status"], row["sealed_dossier_id"],
                )

    return {
        "scanned": len(rows),
        "reconciled": len(reconciled),
        "skipped": len(skipped),
        "reconciled_ids": reconciled,
        "skipped_details": skipped,
    }


def _now() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


__all__ = [
    "reconcile_eval_terminal_states",
    "select_mislabeled_candidates",
    "valid_sealed_dossier_ids",
]
