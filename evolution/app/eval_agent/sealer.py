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
from app.trace.facts import add_lineage, append_score

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
    frozen_evidence: dict[str, Any] | None = None,
) -> str:
    """封存一份不可变评估卷宗。返回评估卷宗 id。

    单事务：写 evaluation_dossiers + 回填 evaluation_sessions.sealed_dossier_id + completed。
    任一步失败 → 整事务回滚，抛 SealError（评估失败，无分裂态）。

    frozen_evidence：本次评估实际引用的证据片段（{evidence_id: 片段内容}）。
    阶段 D：进化 Agent 只读评估卷宗即可归因（需求 §22），无需回钻证据卷宗。
    """
    findings = findings or []
    positive_patterns = positive_patterns or []
    conclusions = conclusions or []
    frozen_evidence = frozen_evidence or {}

    # 1. 完整性校验
    _validate_completeness(findings, positive_patterns)

    dossier_id = _new_dossier_id()
    now = _now()

    # 完整性状态：校验通过即 complete（conclusions 非空 + findings 都有证据）
    completeness = "complete" if (conclusions or findings) else "incomplete"

    conn = db.get_conn()
    try:
        with db._lock:
            session = conn.execute(
                "SELECT self_trace_id FROM evaluation_sessions WHERE eval_id=?",
                (eval_attempt_id,),
            ).fetchone()
            evaluation_trace_id = session["self_trace_id"] if session else None
            cur = conn.execute(
                """INSERT INTO evaluation_dossiers
                   (dossier_id, eval_attempt_id, source_dossier_id, source_dossier_version,
                    trace_id, owner_user_id, conclusions_json, findings_json,
                    positive_patterns_json, scores_json, report_md,
                    frozen_evidence_json, completeness_status, seal_status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sealed', ?)""",
                (
                    dossier_id, eval_attempt_id, source_dossier_id, source_dossier_version,
                    trace_id, owner_user_id,
                    json.dumps(conclusions, ensure_ascii=False),
                    json.dumps(findings, ensure_ascii=False),
                    json.dumps(positive_patterns, ensure_ascii=False),
                    json.dumps(scores, ensure_ascii=False) if scores else None,
                    report_md,
                    json.dumps(frozen_evidence, ensure_ascii=False) if frozen_evidence else None,
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
            add_lineage(
                "evidence_dossier", source_dossier_id, "evaluated_by",
                "evaluation_dossier", dossier_id, conn=conn,
            )
            if evaluation_trace_id:
                add_lineage(
                    "trace", evaluation_trace_id, "produces",
                    "evaluation_dossier", dossier_id, conn=conn,
                )
            if scores is not None:
                score_id = append_score(
                    target_type="trace",
                    target_id=trace_id,
                    rubric_id=str(scores.get("rubric_id") or "xianxia"),
                    rubric_version=str(scores.get("rubric_version") or "unknown"),
                    score=scores,
                    actor_user_id=owner_user_id,
                    conn=conn,
                )
                add_lineage(
                    "evidence_dossier", source_dossier_id, "evaluated_by",
                    "score", score_id, conn=conn,
                )
            # 问题知识库收录（需求 20260731 REQ-01.2）：与封存同事务。
            # 把全部 findings 收录为不可变问题实例并触发候选归并。
            # 非阻塞——ingest 内部整体 try/except，失败不阻断封存（AC-14/AC-46）。
            try:
                from app.problem_kb.ingest import ingest_findings_on_seal
                ingested = ingest_findings_on_seal(
                    dossier_id=dossier_id,
                    trace_id=trace_id,
                    findings=findings,
                    frozen_evidence=frozen_evidence,
                    conn=conn,
                )
                if ingested:
                    logger.info("问题实例收录 %d 条 evd=%s", ingested, dossier_id)
            except Exception:
                # 双保险：ingest 已自吞异常，这里兜底确保绝不阻断封存
                logger.exception("问题实例收录异常 evd=%s（已忽略，不阻断封存）", dossier_id)
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


def collect_frozen_evidence(
    findings: list[dict[str, Any]],
    positive_patterns: list[dict[str, Any]],
    trace_id: str,
    allowed_evidence_ids: set[str],
) -> dict[str, Any]:
    """收集 finding/positive_pattern 引用的证据片段，冻结进评估卷宗（阶段 D）。

    进化 Agent 只读评估卷宗即可归因（需求 §22）。本函数从 event_payloads 取片段，
    但只限 allowed_evidence_ids（证据卷宗 index 登记的 ID，受控）。

    Args:
        findings / positive_patterns: 评估产出（含 evidence_ref/evidence_id）
        trace_id: 证据卷宗来源 trace
        allowed_evidence_ids: 证据卷宗 index 登记的合法 evidence_id 集合（受控回钻边界）
    Returns:
        {evidence_id: 片段摘要}，只含被实际引用且在 index 内的 ID。
    """
    # 收集所有被引用的 evidence_id
    referenced: set[str] = set()
    for item in (*findings, *positive_patterns):
        if not isinstance(item, dict):
            continue
        refs = item.get("evidence_ref") or item.get("evidence_id")
        if isinstance(refs, str):
            refs = [refs]
        if isinstance(refs, list):
            referenced.update(str(r) for r in refs)
    # 只冻结在证据卷宗 index 内的 ID（受控，防注入）
    to_freeze = referenced & allowed_evidence_ids
    if not to_freeze:
        return {}

    frozen: dict[str, Any] = {}
    # event_id 在 payload_json 里（event_payloads 表无 event_id 列），按 trace_id
    # 拉全部事件在 Python 里按 event_id 筛。evolution 量级下可接受。
    rows = db.query_all(
        "SELECT payload_json FROM event_payloads WHERE trace_id = ?",
        (trace_id,),
    )
    # 建 event_id → payload 索引
    by_eid: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        eid = payload.get("event_id")
        if eid:
            by_eid[str(eid)] = payload

    for eid in to_freeze:
        event_id = eid[4:] if eid.startswith("evt-") else eid
        payload = by_eid.get(event_id)
        if payload is None:
            continue
        # 片段摘要：type/agent/sequence/error + tool_output/output（截断）
        snapshot: dict[str, Any] = {
            "type": payload.get("type"),
            "agent_name": payload.get("agent_name"),
            "sequence": payload.get("sequence"),
        }
        if payload.get("error"):
            snapshot["error"] = payload["error"][:300]
        tool_output = payload.get("tool_output")
        if isinstance(tool_output, dict):
            content = tool_output.get("content", "")
            if content:
                snapshot["tool_output"] = str(content)[:1000]
        output = payload.get("output")
        if output:
            snapshot["output"] = str(output)[:1000]
        frozen[eid] = snapshot
    return frozen



__all__ = ["seal_evaluation_dossier", "collect_frozen_evidence", "SealError"]
