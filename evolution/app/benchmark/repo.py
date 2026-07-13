"""benchmark_runs 表的数据访问层（数据闭环设计 C1/D13）。

benchmark_runs 存 case × 版本 × 评估 的矩阵数据，支撑跨版本 leaderboard 对比。
同一 golden_revision 的行之间分数可比（D8 重跑历史保证可比性）。

批次（batch）= 一次触发（发版/升级）产生的全部行，共享 batch_id。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import app.core.db as db

# 状态常量
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_EVALUATING = "evaluating"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

MAX_RETRIES = 3


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_batch_id() -> str:
    return uuid.uuid4().hex[:16]


# ── 批次创建 ────────────────────────────────────────────────


def create_batch(
    *,
    case_ids: list[str],
    versions: list[int],
    golden_revision: str,
) -> str:
    """创建一个 benchmark 批次（case × 版本 的笛卡尔积），返回 batch_id。

    每个组合创建一行 benchmark_runs（pending 状态）。
    """
    batch_id = _new_batch_id()
    now = _now()

    rows = []
    for version in versions:
        for case_id in case_ids:
            rows.append((
                batch_id, case_id, version, golden_revision,
                None, None, None, STATUS_PENDING, 0, None, now, None,
            ))

    if rows:
        db.executemany(
            """INSERT INTO benchmark_runs
               (batch_id, case_id, harness_version, golden_revision,
                trace_id, eval_id, scores_json, status, retries, error,
                ran_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    logger = _get_logger()
    logger.info(
        "创建 benchmark 批次 %s: %d case × %d 版本 = %d 行 (golden_revision=%s)",
        batch_id, len(case_ids), len(versions), len(rows), golden_revision,
    )
    return batch_id


# ── 行状态流转 ──────────────────────────────────────────────


def get_pending(limit: int = 5) -> list[dict[str, Any]]:
    """取待执行的行（runner 用）。按 ran_at ASC 限 limit 条。"""
    return db.query_all(
        """SELECT * FROM benchmark_runs WHERE status=? AND retries < ?
           ORDER BY ran_at ASC LIMIT ?""",
        (STATUS_PENDING, MAX_RETRIES, limit),
    )


def mark_running(run_id: int) -> None:
    db.execute(
        "UPDATE benchmark_runs SET status=? WHERE id=?",
        (STATUS_RUNNING, run_id),
    )


def set_trace(run_id: int, trace_id: str) -> None:
    """跑出 trace 后回填，转 evaluating。"""
    db.execute(
        "UPDATE benchmark_runs SET status=?, trace_id=? WHERE id=?",
        (STATUS_EVALUATING, trace_id, run_id),
    )


def set_result(run_id: int, *, eval_id: str | None, scores_json: str | None) -> None:
    """评估完成，写分数，转 done。"""
    db.execute(
        """UPDATE benchmark_runs
           SET status=?, eval_id=?, scores_json=?, finished_at=?
           WHERE id=?""",
        (STATUS_DONE, eval_id, scores_json, _now(), run_id),
    )


def mark_failed(run_id: int, error: str) -> None:
    """失败：增加 retries，未超 MAX_RETRIES 则回退 pending（可重试）。"""
    row = db.query_one("SELECT retries FROM benchmark_runs WHERE id=?", (run_id,))
    retries = (row["retries"] if row else 0) + 1
    if retries >= MAX_RETRIES:
        status = STATUS_FAILED
    else:
        status = STATUS_PENDING  # 回退待重试
    db.execute(
        "UPDATE benchmark_runs SET status=?, retries=?, error=? WHERE id=?",
        (status, retries, error[:500], run_id),
    )


# ── 查询 ────────────────────────────────────────────────────


def get_batch(batch_id: str) -> dict[str, Any]:
    """查批次状态 + 进度。"""
    rows = db.query_all(
        "SELECT * FROM benchmark_runs WHERE batch_id=? ORDER BY harness_version, case_id",
        (batch_id,),
    )
    if not rows:
        return {"batch_id": batch_id, "status": "not_found", "results": [], "progress": {}}

    total = len(rows)
    done = sum(1 for r in rows if r["status"] == STATUS_DONE)
    failed = sum(1 for r in rows if r["status"] == STATUS_FAILED)
    active = total - done - failed

    if active > 0:
        status = "running"
    elif failed > 0:
        status = "partial" if done > 0 else "failed"
    else:
        status = "done"

    return {
        "batch_id": batch_id,
        "status": status,
        "progress": {"total": total, "done": done, "failed": failed, "active": active},
        "golden_revision": rows[0]["golden_revision"],
        "results": [_row_to_dict(r) for r in rows],
    }


def get_leaderboard(golden_revision: str | None = None) -> dict[str, Any]:
    """跨版本 leaderboard（按 golden_revision 过滤）。

    无 golden_revision 取当前锁定的 revision。
    结构：{ revision, versions: [{ version, cases: [...], avg_score, done_count }] }
    """
    if golden_revision is None:
        from app.dataset import repo as dataset_repo
        golden_revision = dataset_repo.get_golden_revision() or ""

    rows = db.query_all(
        """SELECT * FROM benchmark_runs
           WHERE golden_revision=? AND status=?
           ORDER BY harness_version DESC, case_id""",
        (golden_revision, STATUS_DONE),
    )
    if not rows:
        return {"revision": golden_revision, "versions": [], "case_count": 0}

    # 按 version 聚合
    versions_map: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        ver = row["harness_version"]
        versions_map.setdefault(ver, []).append(_row_to_dict(row))

    versions = []
    for ver in sorted(versions_map.keys(), reverse=True):
        cases = versions_map[ver]
        scores = [c["scores_avg"] for c in cases if c["scores_avg"] is not None]
        versions.append({
            "version": ver,
            "case_count": len(cases),
            "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
            "cases": cases,
        })

    return {
        "revision": golden_revision,
        "versions": versions,
        "case_count": len(versions_map.get(versions[0]["version"], [])) if versions else 0,
    }


def get_recent_versions(k: int = 3) -> list[int]:
    """取最近 K 个版本号（golden 升级重跑用，D18/D20）。从 registry.json。"""
    from app.versioning.registry_repo import list_versions
    versions = list_versions()
    return [v["version"] for v in versions[:k]]


# ── 辅助 ────────────────────────────────────────────────────


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    import json
    scores_avg = None
    if row.get("scores_json"):
        try:
            scores = json.loads(row["scores_json"])
            scores_avg = scores.get("content_overall")
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "id": row["id"],
        "batch_id": row["batch_id"],
        "case_id": row["case_id"],
        "harness_version": row["harness_version"],
        "golden_revision": row["golden_revision"],
        "trace_id": row["trace_id"],
        "eval_id": row["eval_id"],
        "status": row["status"],
        "retries": row["retries"],
        "error": row["error"],
        "scores_avg": scores_avg,
        "ran_at": row["ran_at"],
        "finished_at": row["finished_at"],
    }


def _get_logger():
    import logging
    return logging.getLogger("evolution.benchmark.repo")


__all__ = [
    "STATUS_PENDING", "STATUS_RUNNING", "STATUS_EVALUATING",
    "STATUS_DONE", "STATUS_FAILED", "MAX_RETRIES",
    "create_batch", "get_pending", "mark_running", "set_trace",
    "set_result", "mark_failed",
    "get_batch", "get_leaderboard", "get_recent_versions",
]
