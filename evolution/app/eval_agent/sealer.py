"""评估卷宗封存器（阶段 C，2026-07-27）。

把评估 Agent 的产出（结论 + finding + 正向模式 + 评分 + 引用证据）原子封存为
不可变评估卷宗（evaluation_dossiers 表）。封存是评估成功的唯一标志——
封存失败则评估失败，不产生"评估成功但卷宗未可用"的分裂态（需求 R9）。

职责：
  1. 完整性校验（需求 §30）：每条 finding / positive_pattern 必须携带 evidence_ref
     （指向证据卷宗内的冻结证据 ID）。校验失败 → 抛 SealError，评估失败。
  2. 原子写入 evaluation_dossiers（单事务）+ 回填评估尝试的 sealed_dossier_id + completed。
  3. 绑定证据卷宗版本（source_dossier_id + source_dossier_version 不可变）。

设计依据：.claude/md/20260727_174943_进化证据分级可见性重构.md（§22/§30/R9）
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import app.core.db as db

logger = logging.getLogger("evolution.eval_agent.sealer")


class SealError(Exception):
    """评估卷宗封存失败（完整性校验未过或写入异常）。

    封存失败 = 评估失败（需求 R9），调用方应把评估尝试标 failed。
    """


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_dossier_id() -> str:
    return uuid.uuid4().hex[:12]


def _validate_completeness(
    findings: list[dict[str, Any]],
    positive_patterns: list[dict[str, Any]],
) -> None:
    """完整性校验（需求 §30）。

    每条 finding 和 positive_pattern 必须携带 evidence_ref（引用证据卷宗内冻结证据 ID）。
    缺 evidence_ref 的条目让封存失败——进化 Agent 必须能凭评估卷宗定位根因。
    """
    missing: list[str] = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            missing.append(f"finding[{i}] 非对象")
            continue
        refs = f.get("evidence_ref") or f.get("evidence_id")
        if not refs:
            missing.append(
                f"finding[{i}]({f.get('id', '?')}) 缺 evidence_ref：每条诊断必须引用证据卷宗内的冻结证据"
            )
    for i, p in enumerate(positive_patterns):
        if not isinstance(p, dict):
            missing.append(f"positive_pattern[{i}] 非对象")
            continue
        refs = p.get("evidence_ref") or p.get("evidence_id")
        if not refs:
            missing.append(
                f"positive_pattern[{i}]({p.get('id', '?')}) 缺 evidence_ref：正向模式必须引用证据"
            )
    if missing:
        raise SealError("评估卷宗完整性校验失败：\n" + "\n".join(f"- {m}" for m in missing))


def seal_evaluation_dossier(
    eval_attempt_id: str,
    source_dossier_id: str,
    source_dossier_version: int,
    trace_id: str,
    owner_user_id: str,
    *,
    conclusions: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
    positive_patterns: list[dict[str, Any]] | None = None,
    scores: dict[str, Any] | None = None,
    report_md: str = "",
) -> str:
    """封存一份不可变评估卷宗。返回评估卷宗 id。

    单事务：写 evaluation_dossiers + 回填 evaluation_sessions.sealed_dossier_id + completed。
    任一步失败 → 整事务回滚，抛 SealError（评估失败，无分裂态）。
    """
    findings = findings or []
    positive_patterns = positive_patterns or []
    conclusions = conclusions or []

    # 1. 完整性校验
    _validate_completeness(findings, positive_patterns)

    dossier_id = _new_dossier_id()
    now = _now()

    # 完整性状态：校验通过即 complete（conclusions 非空 + findings 都有证据）
    completeness = "complete" if (conclusions or findings) else "incomplete"

    conn = db.get_conn()
    try:
        with db._lock:
            cur = conn.execute(
                """INSERT INTO evaluation_dossiers
                   (dossier_id, eval_attempt_id, source_dossier_id, source_dossier_version,
                    trace_id, owner_user_id, conclusions_json, findings_json,
                    positive_patterns_json, scores_json, report_md,
                    completeness_status, seal_status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sealed', ?)""",
                (
                    dossier_id, eval_attempt_id, source_dossier_id, source_dossier_version,
                    trace_id, owner_user_id,
                    json.dumps(conclusions, ensure_ascii=False),
                    json.dumps(findings, ensure_ascii=False),
                    json.dumps(positive_patterns, ensure_ascii=False),
                    json.dumps(scores, ensure_ascii=False) if scores else None,
                    report_md,
                    completeness, now,
                ),
            )
            if cur.rowcount != 1:
                raise SealError(f"写入 evaluation_dossiers 失败（rowcount={cur.rowcount}）")

            # 回填评估尝试：sealed_dossier_id + status=completed
            conn.execute(
                "UPDATE evaluation_sessions SET sealed_dossier_id=?, status='completed', updated_at=? "
                "WHERE eval_id=?",
                (dossier_id, now, eval_attempt_id),
            )
            conn.commit()
    except SealError:
        conn.rollback()
        raise
    except db.sqlite3.IntegrityError as exc:
        # UNIQUE(eval_attempt_id) 冲突：一个尝试最多一份卷宗（需求 §37）
        conn.rollback()
        logger.warning("评估卷宗封存被拒（尝试 %s 已有卷宗）：%s", eval_attempt_id, exc)
        raise SealError(f"该评估尝试已封存过卷宗（一个尝试最多一份评估卷宗）") from exc
    except Exception as exc:
        conn.rollback()
        logger.exception("评估卷宗封存异常 attempt=%s", eval_attempt_id)
        raise SealError(f"封存评估卷宗异常：{exc}") from exc

    logger.info(
        "评估卷宗封存成功: evd=%s attempt=%s source_dossier=%s(v%s) findings=%d",
        dossier_id, eval_attempt_id, source_dossier_id, source_dossier_version, len(findings),
    )
    return dossier_id


__all__ = ["seal_evaluation_dossier", "SealError"]
