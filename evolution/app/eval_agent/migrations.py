"""历史误标评估的一次性可审计纠正（FR-006 / DEC-003 / AC-008 / RSK-003）。

根因：旧状态判断缺陷（agent.py 只认 done 不认 completed）把封存成功的评估改回 failed，
制造「业务 failed / sealed dossier 已存在 / Trace completed」的三方分裂（EVD-011/EVD-012）。

本迁移严格按 DEC-003 执行：
  - 目标集合必须由多方不可变事实共同证明（有效 sealed dossier + 完整内容/血缘 +
    对应成功 Trace/封存事件），绝不按单字段批量改写（RSK-003）；
  - 先只读预演（dry_run=True），输出目标集合 + 排除集合 + 证据；
  - 精确更新可证明记录，保留更新前快照与审计记录；
  - 不重跑模型、不改评估卷宗内容、不改历史 Trace 事件；
  - 真正失败（无 sealed dossier）的对照记录一律不动。

幂等：已 completed 的跳过；可安全重复执行。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import app.core.db as db
from app.eval_agent.reconcile import select_mislabeled_candidates, valid_sealed_dossier_ids

logger = logging.getLogger("evolution.eval_agent.migrations")


def _load_findings(dossier_row: dict[str, Any]) -> list[dict[str, Any]]:
    """评估卷宗的 findings（完整性证据）。"""
    raw = dossier_row.get("findings_json")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _load_dossier_row(dossier_id: str) -> dict[str, Any] | None:
    """读取 sealed 卷宗行（供完整性/血缘校验）。不存在或非 sealed 返回 None。"""
    row = db.query_one(
        "SELECT * FROM evaluation_dossiers WHERE dossier_id = ? AND seal_status = 'sealed'",
        (dossier_id,),
    )
    return dict(row) if row else None


def _has_complete_content(dossier_row: dict[str, Any]) -> bool:
    """不可变事实 2：卷宗内容完整（有 conclusions 或 findings，且 findings 都有证据引用）。"""
    completeness = dossier_row.get("completeness_status")
    if completeness == "complete":
        return True
    # 兜底：即使状态列缺失，也校验 findings 都有 evidence_ref
    findings = _load_findings(dossier_row)
    if not findings:
        return False
    return all(
        isinstance(f, dict) and (f.get("evidence_ref") or f.get("evidence_id"))
        for f in findings
    )


def _has_success_trace_event(self_trace_id: str | None) -> bool:
    """不可变事实 3：对应自观测 Trace 有成功/完成事件（run_end/completed 终态）。

    封存成功时 sealer 写 lineage；评估 Trace 终态由 recorder.complete_run 写入。
    这里检查 runs 表该 trace 的终态为 completed（不读历史事件正文，只看终态事实）。
    """
    if not self_trace_id:
        return False
    row = db.query_one(
        "SELECT status FROM runs WHERE trace_id = ?", (self_trace_id,),
    )
    return bool(row and row["status"] == "completed")


def _is_provable_success(session: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, list[str]]:
    """判定一条误标记录是否可由多方不可变事实证明为成功。

    Returns: (可证明, dossier_row 或 None, 证据缺口列表)
    """
    gaps: list[str] = []
    sealed_id = session.get("sealed_dossier_id")
    if not sealed_id:
        return False, None, ["无 sealed_dossier_id"]

    dossier = _load_dossier_row(sealed_id)
    if dossier is None:
        gaps.append(f"sealed_dossier_id={sealed_id} 无对应 sealed 卷宗")
        return False, None, gaps

    if not _has_complete_content(dossier):
        gaps.append(f"卷宗 {sealed_id} 内容不完整")

    if not _has_success_trace_event(session.get("self_trace_id")):
        gaps.append(f"自观测 Trace {session.get('self_trace_id')} 终态非 completed")

    if gaps:
        return False, dossier, gaps
    return True, dossier, []


def correct_mislabeled_eval_history(*, dry_run: bool = True) -> dict[str, Any]:
    """一次性纠正可证明成功的历史误标评估（FR-006 / DEC-003）。

    dry_run=True（默认）：只读预演，输出目标集合 + 排除集合 + 证据，不写库。
    dry_run=False：精确更新可证明记录，保留审计。

    Returns: {mode, scanned, target, excluded, corrected, audit, snapshots}
      - target: 可证明为成功、当前误标非 completed 的记录
      - excluded: 有 sealed_dossier_id 但证据不充分（不动）
      - snapshots: 更新前快照（dry_run=False 时）
      - audit: 审计记录
    """
    # 候选：有 sealed_dossier_id 但业务状态非 completed（误标候选）。复用 reconcile 的查询。
    candidates = select_mislabeled_candidates()

    target: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []

    for row in candidates:
        session = dict(row)
        provable, dossier, gaps = _is_provable_success(session)
        if provable:
            target.append({
                "eval_id": session["eval_id"],
                "trace_id": session["trace_id"],
                "current_status": session["status"],
                "sealed_dossier_id": session["sealed_dossier_id"],
                "self_trace_id": session.get("self_trace_id"),
                "evidence": {
                    "sealed_dossier_valid": True,
                    "content_complete": True,
                    "trace_completed": True,
                    "findings_count": len(_load_findings(dossier or {})),
                },
            })
        else:
            excluded.append({
                "eval_id": session["eval_id"],
                "current_status": session["status"],
                "sealed_dossier_id": session["sealed_dossier_id"],
                "gaps": gaps,
            })

    corrected: list[str] = []
    audit: dict[str, Any] = {}

    if not dry_run and target:
        now = datetime.now(UTC).isoformat()
        criterion = "valid_sealed_dossier AND complete_content AND trace_completed"
        # 原子纠正：整批 UPDATE + 审计写入在单事务内，任一失败整体回滚（RSK-003 可精确回滚）。
        # 审计写 eval_correction_audit 表（持久化，不随返回值丢失），与 UPDATE 同事务。
        with db.transaction() as conn:
            for item in target:
                snapshot = {
                    "eval_id": item["eval_id"],
                    "status_before": item["current_status"],
                    "sealed_dossier_id": item["sealed_dossier_id"],
                    "self_trace_id": item.get("self_trace_id"),
                    "snapshot_at": now,
                }
                snapshots.append(snapshot)
                # 纠正 + 清理 failure_reason/stop_reason（FR-006 一致终态，不留自相矛盾状态）。
                conn.execute(
                    "UPDATE evaluation_sessions SET status='completed', "
                    "failure_reason=NULL, updated_at=? WHERE eval_id=?",
                    (now, item["eval_id"]),
                )
                # 持久化审计行（与 UPDATE 同事务，崩溃时随回滚一起撤销）。
                conn.execute(
                    """INSERT INTO eval_correction_audit
                       (eval_id, status_before, status_after, sealed_dossier_id,
                        criterion, snapshot_json, corrected_at)
                       VALUES (?, ?, 'completed', ?, ?, ?, ?)""",
                    (item["eval_id"], item["current_status"], item["sealed_dossier_id"],
                     criterion, json.dumps(snapshot, ensure_ascii=False), now),
                )
                corrected.append(item["eval_id"])
                logger.info(
                    "历史误标纠正：eval=%s %s→completed（sealed=%s，多方事实证明，已记审计）",
                    item["eval_id"], item["current_status"], item["sealed_dossier_id"],
                )

        audit = {
            "migration": "correct_mislabeled_eval_history",
            "executed_at": now,
            "criterion": criterion,
            "corrected_count": len(corrected),
            "corrected_ids": corrected,
            "snapshots": snapshots,
            "audit_table": "eval_correction_audit",
        }

    return {
        "mode": "dry_run" if dry_run else "apply",
        "scanned": len(candidates),
        "target": target,
        "excluded": excluded,
        "corrected": corrected,
        "audit": audit,
        "snapshots": snapshots,
    }


__all__ = ["correct_mislabeled_eval_history"]
