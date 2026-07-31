"""问题知识库 5 张表的数据访问层（DAL）。

仿 app/reflection/repo.py 风格（import app.core.db as db）。

事务边界约定（AC-46 并发安全 / 收录不阻断封存）：
  - 默认：每个写函数自管理事务，走 db.execute（自动 commit）。
  - 收录场景（封存同事务）：传 conn= 复用 sealer 的连接，保证问题实例与评估卷宗原子写入。
    此时调用方负责 commit/rollback；本层不在 conn 模式下自行 commit。

所有读写都支持 conn= 参数（sqlite3.Connection | None）：
  - conn 非 None：直接用 conn 执行，返回结果（不 commit，交给调用方）。
  - conn None：走 db.execute / db.query_*（自动事务）。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

import app.core.db as db


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    """生成带前缀的 id（如 pinst-<uuid12>）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _exec(sql: str, params: Any, conn: sqlite3.Connection | None) -> sqlite3.Cursor:
    """统一执行：conn 模式直接执行（不 commit），否则走 db.execute。"""
    if conn is not None:
        return conn.execute(sql, params)
    return db.execute(sql, params)


def _query_all(sql: str, params: Any, conn: sqlite3.Connection | None) -> list[dict[str, Any]]:
    if conn is not None:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    return db.query_all(sql, params)


def _query_one(sql: str, params: Any, conn: sqlite3.Connection | None) -> dict[str, Any] | None:
    rows = _query_all(sql, params, conn)
    return rows[0] if rows else None


# ════════════════════════════════════════════════════════════════
# problem_instances —— 不可变问题实例账本（REQ-01.1）
# ════════════════════════════════════════════════════════════════


def create_instance(
    *,
    dossier_id: str,
    trace_id: str,
    finding_id: str,
    severity: str,
    statement: str,
    dimension: str = "未分类",
    frozen_evidence_ref: list[str] | None = None,
    classification: dict[str, Any] | None = None,
    raw_description: str = "",
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """收录一个问题实例。返回 instance_id；若已存在（UNIQUE 冲突）返回 None（幂等，AC-42）。

    幂等语义：同 (dossier_id, finding_id) 重复收录视为已存在，不报错、不覆盖。
    归并不改写实例事实（REQ-01.1）—— 已存在的行原样保留。
    """
    instance_id = _new_id("pinst")
    cur = _exec(
        """INSERT INTO problem_instances
           (instance_id, dossier_id, trace_id, finding_id, severity, dimension,
            statement, frozen_evidence_ref, classification_json, raw_description, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(dossier_id, finding_id) DO NOTHING""",
        (
            instance_id, dossier_id, trace_id, finding_id, severity, dimension,
            statement,
            json.dumps(frozen_evidence_ref, ensure_ascii=False) if frozen_evidence_ref else None,
            json.dumps(classification, ensure_ascii=False) if classification else None,
            raw_description, _now(),
        ),
        conn,
    )
    # ON CONFLICT DO NOTHING 时 rowcount=0，说明已存在
    if cur.rowcount == 0:
        return None
    return instance_id


def get_instance(instance_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM problem_instances WHERE instance_id=?", (instance_id,))


def get_instance_by_finding(dossier_id: str, finding_id: str) -> dict[str, Any] | None:
    """按 (dossier_id, finding_id) 取实例（幂等收录后反查）。"""
    return db.query_one(
        "SELECT * FROM problem_instances WHERE dossier_id=? AND finding_id=?",
        (dossier_id, finding_id),
    )


def list_by_dossier(dossier_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT * FROM problem_instances WHERE dossier_id=? ORDER BY finding_id",
        (dossier_id,),
    )


def list_by_trace(trace_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT * FROM problem_instances WHERE trace_id=? ORDER BY created_at",
        (trace_id,),
    )


def list_all(
    *,
    severity: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """列实例（治理/展示用，可按严重度过滤）。"""
    if severity:
        return db.query_all(
            "SELECT * FROM problem_instances WHERE severity=? ORDER BY created_at DESC LIMIT ?",
            (severity, limit),
        )
    return db.query_all(
        "SELECT * FROM problem_instances ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )


# ════════════════════════════════════════════════════════════════
# standard_problems —— 可治理标准问题库（REQ-01.4 / DEC-28）
# ════════════════════════════════════════════════════════════════


def create_problem(
    *,
    title: str,
    description: str = "",
    classification: dict[str, Any] | None = None,
    severity: str = "未分类",
    conn: sqlite3.Connection | None = None,
) -> str:
    """新建标准问题（初始 lifecycle=开放，formal_frequency=0，REQ-08.1）。

    注意：标准问题应由用户确认后形成（DEC-28）。本函数供治理确认流程调用，
    非 Agent 自动创建。
    """
    problem_id = _new_id("sprob")
    now = _now()
    _exec(
        """INSERT INTO standard_problems
           (problem_id, title, description, classification_json, severity,
            lifecycle_status, formal_frequency, suspect_count, retrieval_count,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, '开放', 0, 0, 0, ?, ?)""",
        (
            problem_id, title, description,
            json.dumps(classification, ensure_ascii=False) if classification else None,
            severity, now, now,
        ),
        conn,
    )
    return problem_id


def get_problem(problem_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM standard_problems WHERE problem_id=?", (problem_id,))


def list_problems(
    *,
    lifecycle_status: str | None = None,
    severity: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """列标准问题（治理工作台用，可按生命周期/严重度筛选）。"""
    clauses: list[str] = []
    params: list[Any] = []
    if lifecycle_status:
        clauses.append("lifecycle_status=?")
        params.append(lifecycle_status)
    if severity:
        clauses.append("severity=?")
        params.append(severity)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return db.query_all(
        f"SELECT * FROM standard_problems{where} ORDER BY updated_at DESC LIMIT ?",
        tuple(params),
    )


def update_lifecycle(problem_id: str, status: str) -> None:
    """更新生命周期状态（REQ-08，治理/效果验证驱动）。"""
    db.execute(
        "UPDATE standard_problems SET lifecycle_status=?, updated_at=? WHERE problem_id=?",
        (status, _now(), problem_id),
    )


def update_severity(problem_id: str, severity: str) -> None:
    db.execute(
        "UPDATE standard_problems SET severity=?, updated_at=? WHERE problem_id=?",
        (severity, _now(), problem_id),
    )


def recalc_formal_frequency(problem_id: str) -> int:
    """重算正式频率 = 已确认关联（problem_instance_links）的独立 trace 数（REQ-01.5/DEC-09）。

    同一 trace 无论被重复评估多少次、产生多少同义 findings，对同一标准问题最多贡献 1。
    """
    row = db.query_one(
        """SELECT COUNT(DISTINCT pi.trace_id) AS n
           FROM problem_instance_links pil
           JOIN problem_instances pi ON pil.instance_id = pi.instance_id
           WHERE pil.problem_id=?""",
        (problem_id,),
    )
    freq = row["n"] if row else 0
    db.execute(
        "UPDATE standard_problems SET formal_frequency=?, updated_at=? WHERE problem_id=?",
        (freq, _now(), problem_id),
    )
    return freq


def recalc_suspect_count(problem_id: str) -> int:
    """重算疑似出现数 = 指向该标准问题的待确认候选数（REQ-01.5）。"""
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM problem_merge_candidates WHERE target_problem_id=? AND status='pending'",
        (problem_id,),
    )
    count = row["n"] if row else 0
    db.execute(
        "UPDATE standard_problems SET suspect_count=?, updated_at=? WHERE problem_id=?",
        (count, _now(), problem_id),
    )
    return count


def increment_retrieval(problem_id: str) -> None:
    """被检索命中时 +1（利用率统计，REQ-01.5）。"""
    db.execute(
        "UPDATE standard_problems SET retrieval_count=retrieval_count+1, updated_at=? WHERE problem_id=?",
        (_now(), problem_id),
    )


# ════════════════════════════════════════════════════════════════
# problem_merge_candidates —— 候选归并（REQ-03 / DEC-05）
# ════════════════════════════════════════════════════════════════


def create_candidate(
    *,
    instance_id: str,
    target_problem_id: str | None = None,
    is_new_problem_proposal: bool = False,
    match_method: str = "",
    match_model_version: str = "",
    confidence: float = 0.0,
    match_evidence: str = "",
    conn: sqlite3.Connection | None = None,
) -> str:
    """创建候选归并（待确认，REQ-03.1）。新标准问题候选时 target_problem_id 为空。"""
    candidate_id = _new_id("cand")
    _exec(
        """INSERT INTO problem_merge_candidates
           (candidate_id, instance_id, target_problem_id, is_new_problem_proposal,
            match_method, match_model_version, confidence, match_evidence,
            status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (
            candidate_id, instance_id, target_problem_id,
            1 if is_new_problem_proposal else 0,
            match_method, match_model_version, confidence, match_evidence, _now(),
        ),
        conn,
    )
    return candidate_id


def list_pending(
    *,
    limit: int = 100,
    severity: str | None = None,
) -> list[dict[str, Any]]:
    """列待确认候选（治理工作台队列，REQ-07）。可按实例严重度筛选（AC-44）。"""
    if severity:
        return db.query_all(
            """SELECT cand.*, pi.severity AS instance_severity, pi.statement AS instance_statement
               FROM problem_merge_candidates cand
               JOIN problem_instances pi ON cand.instance_id = pi.instance_id
               WHERE cand.status='pending' AND pi.severity=?
               ORDER BY cand.created_at DESC LIMIT ?""",
            (severity, limit),
        )
    return db.query_all(
        """SELECT cand.*, pi.severity AS instance_severity, pi.statement AS instance_statement
           FROM problem_merge_candidates cand
           JOIN problem_instances pi ON cand.instance_id = pi.instance_id
           WHERE cand.status='pending'
           ORDER BY cand.created_at DESC LIMIT ?""",
        (limit,),
    )


def list_candidates_for_instance(instance_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT * FROM problem_merge_candidates WHERE instance_id=? ORDER BY created_at DESC",
        (instance_id,),
    )


def confirm_candidate(candidate_id: str, *, decided_by: str) -> dict[str, Any] | None:
    """确认候选归并（REQ-03.4/AC-04）：写 link + 重算频率 + 标记候选 confirmed。

    新标准问题候选确认时，会先建标准问题（调用方负责），再把 target_problem_id 回填。
    返回更新后的候选行。
    """
    cand = db.query_one(
        "SELECT * FROM problem_merge_candidates WHERE candidate_id=?", (candidate_id,)
    )
    if not cand:
        return None
    instance_id = cand["instance_id"]
    problem_id = cand["target_problem_id"]
    if not problem_id:
        return None  # 新标准问题候选须先由调用方建标准问题并回填 target

    now = _now()
    # 写已确认 link（UNIQUE(instance_id) 冲突说明该实例已归并过——视为 superseded）
    try:
        db.execute(
            """INSERT INTO problem_instance_links (link_id, instance_id, problem_id, confirmed_by, confirmed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (_new_id("link"), instance_id, problem_id, decided_by, now),
        )
    except db.sqlite3.IntegrityError:
        # 该实例已归并到别的问题——本候选标 superseded，不改写既有归属
        db.execute(
            "UPDATE problem_merge_candidates SET status='superseded', decided_by=?, decided_at=? WHERE candidate_id=?",
            (decided_by, now, candidate_id),
        )
        return get_candidate(candidate_id)

    db.execute(
        "UPDATE problem_merge_candidates SET status='confirmed', decided_by=?, decided_at=? WHERE candidate_id=?",
        (decided_by, now, candidate_id),
    )
    recalc_formal_frequency(problem_id)
    recalc_suspect_count(problem_id)
    return get_candidate(candidate_id)


def reject_candidate(candidate_id: str, *, decided_by: str) -> dict[str, Any] | None:
    """否决候选（REQ-03.4/AC-05）：保留实例与否决事实，不影响正式频率。

    否决后仍保留问题实例及否决事实，并可继续形成其他候选（REQ-03.5）。
    """
    now = _now()
    db.execute(
        "UPDATE problem_merge_candidates SET status='rejected', decided_by=?, decided_at=? WHERE candidate_id=?",
        (decided_by, now, candidate_id),
    )
    # 若指向某标准问题，重算其疑似计数
    cand = get_candidate(candidate_id)
    if cand and cand.get("target_problem_id"):
        recalc_suspect_count(cand["target_problem_id"])
    return cand


def get_candidate(candidate_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM problem_merge_candidates WHERE candidate_id=?", (candidate_id,)
    )


def set_candidate_target(candidate_id: str, problem_id: str) -> None:
    """为新标准问题候选回填确认后的标准问题 id（治理确认流程用）。"""
    db.execute(
        "UPDATE problem_merge_candidates SET target_problem_id=? WHERE candidate_id=?",
        (problem_id, candidate_id),
    )


# ════════════════════════════════════════════════════════════════
# problem_instance_links —— 已确认归并关系（REQ-01.4）
# ════════════════════════════════════════════════════════════════


def link_instance(*, instance_id: str, problem_id: str, confirmed_by: str) -> str | None:
    """直接建立已确认归并（治理拆分/合并流程用）。返回 link_id，已存在返回 None。"""
    try:
        link_id = _new_id("link")
        db.execute(
            """INSERT INTO problem_instance_links (link_id, instance_id, problem_id, confirmed_by, confirmed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (link_id, instance_id, problem_id, confirmed_by, _now()),
        )
        return link_id
    except db.sqlite3.IntegrityError:
        return None


def get_link_for_instance(instance_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM problem_instance_links WHERE instance_id=?", (instance_id,)
    )


def list_links_for_problem(problem_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT * FROM problem_instance_links WHERE problem_id=? ORDER BY confirmed_at",
        (problem_id,),
    )


def unlink_instance(instance_id: str, *, decided_by: str = "") -> bool:
    """取消归并（拆分错误归并用，AC-01/AC-05）。问题实例内容不变。

    返回是否实际移除。频率随后由调用方 recalc。
    """
    cur = db.execute(
        "DELETE FROM problem_instance_links WHERE instance_id=?", (instance_id,)
    )
    return cur.rowcount > 0


# ════════════════════════════════════════════════════════════════
# evolution_point_ownership —— 进化点一对一归属（REQ-01.3/DEC-20/AC-33）
# ════════════════════════════════════════════════════════════════


def assign_ownership(
    *,
    point_id: str,
    problem_id: str | None = None,
    source_instance_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """登记进化点归属（一对一，REQ-01.3）。

    point_id 是 PK，重复登记会因主键冲突而忽略（已归属则不动）。
    返回是否新写入。
    """
    try:
        _exec(
            """INSERT INTO evolution_point_ownership (point_id, problem_id, source_instance_id, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(point_id) DO NOTHING""",
            (point_id, problem_id, source_instance_id, _now()),
            conn,
        )
        return True
    except db.sqlite3.IntegrityError:
        return False


def get_ownership(point_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM evolution_point_ownership WHERE point_id=?", (point_id,)
    )


def list_ownership_for_problem(problem_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT * FROM evolution_point_ownership WHERE problem_id=? ORDER BY created_at",
        (problem_id,),
    )


# ════════════════════════════════════════════════════════════════
# current_problem_cards —— 当前问题卡冻结快照（REQ-04.8/DEC-15/AC-27）
# ════════════════════════════════════════════════════════════════


def create_card(
    *,
    session_id: str,
    problem_group: str,
    frozen_snapshot: dict[str, Any],
    instance_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """冻结一张当前问题卡（不可变快照，AC-27）。"""
    card_id = _new_id("card")
    _exec(
        """INSERT INTO current_problem_cards
           (card_id, session_id, instance_id, problem_group, frozen_snapshot_json, retrieval_state, created_at)
           VALUES (?, ?, ?, ?, ?, 'frozen', ?)""",
        (
            card_id, session_id, instance_id, problem_group,
            json.dumps(frozen_snapshot, ensure_ascii=False), _now(),
        ),
        conn,
    )
    return card_id


def update_card_retrieval_state(card_id: str, state: str) -> None:
    """检索后追加状态（frozen→retrieved/degraded，不改快照本身，AC-27）。"""
    db.execute(
        "UPDATE current_problem_cards SET retrieval_state=? WHERE card_id=?",
        (state, card_id),
    )


def list_cards_for_session(session_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT * FROM current_problem_cards WHERE session_id=? ORDER BY created_at",
        (session_id,),
    )


__all__ = [
    # problem_instances
    "create_instance", "get_instance", "get_instance_by_finding",
    "list_by_dossier", "list_by_trace", "list_all",
    # standard_problems
    "create_problem", "get_problem", "list_problems",
    "update_lifecycle", "update_severity",
    "recalc_formal_frequency", "recalc_suspect_count", "increment_retrieval",
    # candidates
    "create_candidate", "list_pending", "list_candidates_for_instance",
    "confirm_candidate", "reject_candidate", "get_candidate", "set_candidate_target",
    # links
    "link_instance", "get_link_for_instance", "list_links_for_problem", "unlink_instance",
    # ownership
    "assign_ownership", "get_ownership", "list_ownership_for_problem",
    # cards
    "create_card", "update_card_retrieval_state", "list_cards_for_session",
]
