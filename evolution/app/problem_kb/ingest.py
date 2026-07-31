"""问题实例收录（封存评估卷宗时触发，REQ-01.2 / REQ-03 / AC-14 / AC-46）。

触发点：sealer.seal_evaluation_dossier 单事务内，INSERT evaluation_dossiers 成功后、
commit 前。本模块的写入与封存共用同一连接（conn=），保证问题实例与评估卷宗原子可见。

非阻塞契约（REQ-03 边界 / AC-14 / AC-46）：
  - 收录或候选生成失败时，问题实例事实必须保留，不能因为知识归并失败而使评估或进化
    结果丢失。
  - 实现：本函数整体 try/except 包裹，任何异常仅记日志，绝不向上抛出。封存事务照常提交。
  - 注：因与封存同连接，若本函数内部抛错已 execute 的语句会随封存 commit 一起落地，
    未执行的部分丢失但可后续对账补录（R9）。

收录逻辑（REQ-01.2 / DEC-06）：
  1. 封存评估卷宗中的全部 findings 都形成问题实例。
  2. 中、高严重度实例默认参与候选归并；低严重度保留但默认不参与候选。
  3. 候选生成非阻塞（B2 matcher）。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from app.problem_kb import repo
from app.problem_kb.classifier import classify_finding

logger = logging.getLogger("evolution.problem_kb.ingest")

# 默认参与候选归并的严重度（DEC-06：中高严重度，低严重度默认折叠）
_CANDIDATE_SEVERITIES = frozenset({"high", "medium"})


def ingest_findings_on_seal(
    *,
    dossier_id: str,
    trace_id: str,
    findings: list[dict[str, Any]],
    frozen_evidence: dict[str, Any] | None,
    conn: sqlite3.Connection,
) -> int:
    """封存时收录全部 findings 为问题实例（REQ-01.2），并触发候选归并（REQ-03）。

    与封存同连接（conn）执行，不在本函数内 commit（交由 sealer 的 conn.commit()）。
    失败仅记日志，不抛出（AC-14 / AC-46 非阻塞）。

    Returns:
        本次新收录的实例数（已存在的因幂等跳过）。
    """
    new_count = 0
    try:
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            finding_id = str(finding.get("id") or "")
            if not finding_id:
                continue
            severity = str(finding.get("severity") or "low")
            statement = str(finding.get("finding") or "")
            dimension = str(finding.get("dimension") or "未分类")

            # 多轴分类（REQ-02 / AC-32）
            classification = classify_finding(finding, frozen_evidence)

            # 证据引用快照（受控回钻边界，不改写原证据）
            refs_raw = finding.get("evidence_ref") or finding.get("evidence_id") or []
            if isinstance(refs_raw, str):
                refs = [refs_raw]
            else:
                refs = [str(r) for r in refs_raw]

            instance_id = repo.create_instance(
                dossier_id=dossier_id,
                trace_id=trace_id,
                finding_id=finding_id,
                severity=severity,
                statement=statement,
                dimension=dimension,
                frozen_evidence_ref=refs,
                classification=classification,
                raw_description=statement,
                conn=conn,
            )
            if instance_id is None:
                continue  # 幂等：已存在（AC-42）

            new_count += 1

            # 中高严重度 → 触发候选归并（REQ-03 / DEC-06）。低严重度默认折叠不参与。
            if severity in _CANDIDATE_SEVERITIES:
                _try_generate_candidates(instance_id, finding, classification, conn)

    except Exception:
        # 非阻塞：收录失败不阻断封存（AC-14/AC-46）。问题实例事实可在后续对账补录。
        logger.exception(
            "问题实例收录异常 dossier=%s（已忽略，不阻断封存）", dossier_id
        )
    return new_count


def _try_generate_candidates(
    instance_id: str,
    finding: dict[str, Any],
    classification: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    """对一个新实例生成候选归并（REQ-03）。失败仅记日志（AC-14）。

    延迟 import 避免循环依赖（matcher 依赖 retrieval，retrieval 依赖 store）。
    """
    try:
        from app.problem_kb.retrieval import matcher
        matcher.generate_candidates(
            instance_id=instance_id,
            finding=finding,
            classification=classification,
            conn=conn,
        )
    except Exception:
        logger.exception(
            "候选归并生成异常 instance=%s（已忽略，实例事实已保留）", instance_id
        )


__all__ = ["ingest_findings_on_seal"]
