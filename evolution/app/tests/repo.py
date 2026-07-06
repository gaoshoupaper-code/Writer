"""manual_tests 表的数据访问层。

字段约定见 db.py 的建表语句（决策 D-Q7）。
状态机：pending → running → done | failed | cancelled（决策 D10 + 停止功能）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import app.core.db as db


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_test(
    *,
    case_id: str,
    version_type: str,
    version_id: int | None,
    retry_of: str | None = None,
    origin_layer: str | None = None,
) -> str:
    """创建一条 pending 测试记录，返回 test_id。

    origin_layer（决策 A6）：标记本次测试跑在哪个数据集层（golden|growing），
    进化 Agent 据此区分验证 vs 探索。None 时由调用方从 evalset 推导后传入。
    """
    test_id = uuid.uuid4().hex[:16]
    db.execute(
        """INSERT INTO manual_tests
           (test_id, case_id, version_type, version_id, trace_id, task_id,
            status, error, retry_of, created_at, origin_layer)
           VALUES (?, ?, ?, ?, NULL, NULL, 'pending', NULL, ?, ?, ?)""",
        (test_id, case_id, version_type, version_id, retry_of, _now(), origin_layer),
    )
    return test_id


def get_test(test_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM manual_tests WHERE test_id=?", (test_id,))


def list_tests(
    *,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """列测试记录（created_at 倒序，分页）。status=None 表示全部。"""
    where = ""
    params: list[Any] = []
    if status and status != "全部":
        where = "WHERE status=?"
        params.append(status)

    total_row = db.query_one(f"SELECT COUNT(*) AS n FROM manual_tests {where}", tuple(params))
    total = total_row["n"] if total_row else 0

    offset = (page - 1) * page_size
    params.extend([page_size, offset])
    rows = db.query_all(
        f"""SELECT * FROM manual_tests {where}
            ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        tuple(params),
    )
    return rows, total


def mark_running(test_id: str, task_id: str) -> None:
    """拿到 executor task_id 后转 running。"""
    db.execute(
        "UPDATE manual_tests SET status='running', task_id=? WHERE test_id=?",
        (task_id, test_id),
    )


def set_trace_id(test_id: str, trace_id: str) -> None:
    """后台轮询拿到 trace_id 后回填（done/failed 之前）。"""
    db.execute(
        "UPDATE manual_tests SET trace_id=? WHERE test_id=? AND trace_id IS NULL",
        (trace_id, test_id),
    )


def mark_done(test_id: str, trace_id: str) -> None:
    """trace 摄入完成（completed）→ done。"""
    db.execute(
        "UPDATE manual_tests SET status='done', trace_id=? WHERE test_id=?",
        (trace_id, test_id),
    )


def mark_failed(test_id: str, error: str, trace_id: str | None = None) -> None:
    """失败 → failed。trace_id 可选（trace 已建后失败时填）。"""
    db.execute(
        "UPDATE manual_tests SET status='failed', error=?, trace_id=COALESCE(?, trace_id) WHERE test_id=?",
        (error, trace_id, test_id),
    )


def mark_cancelled(test_id: str, trace_id: str | None = None) -> None:
    """用户主动停止 → cancelled（终态）。

    与 failed 区分：cancelled 是用户意图，非异常。trace_id 可选（pending 阶段停
    可能还没建 trace）。注意：executor 侧 cancel_run 已把 trace 收尾成 cancelled，
    evolution 的 ingest 链路会把该 trace 状态同步到 runs 表。
    """
    db.execute(
        "UPDATE manual_tests SET status='cancelled', trace_id=COALESCE(?, trace_id) WHERE test_id=?",
        (trace_id, test_id),
    )


def find_by_trace_id(trace_id: str) -> dict[str, Any] | None:
    """按 trace_id 反查测试记录（ingest 通知驱动状态同步用）。"""
    return db.query_one("SELECT * FROM manual_tests WHERE trace_id=?", (trace_id,))


def find_by_task_id(task_id: str) -> dict[str, Any] | None:
    """按 task_id 反查测试记录（task 失败无 trace 兜底通知用）。"""
    return db.query_one("SELECT * FROM manual_tests WHERE task_id=?", (task_id,))


def find_pending_by_task_id(task_id: str) -> dict[str, Any] | None:
    """按 task_id 反查仍在 running 的测试记录（兜底通知时只更新未终结的）。"""
    return db.query_one(
        "SELECT * FROM manual_tests WHERE task_id=? AND status IN ('pending','running')",
        (task_id,),
    )


def delete_test(test_id: str) -> bool:
    """删除一条测试记录（仅引用行，不删 trace 真数据——由 api 层负责级联）。

    Returns:
        True 表示删到了记录，False 表示记录不存在。
    """
    cur = db.execute(
        "DELETE FROM manual_tests WHERE test_id=?", (test_id,)
    )
    return cur.rowcount > 0


__all__ = [
    "create_test",
    "get_test",
    "list_tests",
    "mark_running",
    "set_trace_id",
    "mark_done",
    "mark_failed",
    "mark_cancelled",
    "delete_test",
    "find_by_trace_id",
    "find_by_task_id",
    "find_pending_by_task_id",
]
