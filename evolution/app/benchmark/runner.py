"""Benchmark Runner — 后台矩阵执行（数据闭环设计 C2/D12）。

对 case × 版本 笛卡尔积逐个执行：
  1. 调 executor /internal/ab/run（传 demand_md + 版本配置）
  2. 轮询 /internal/ab/status 直到 done/failed
  3. 调 eval_agent/scoring 评估
  4. 写 benchmark_runs

后台异步（asyncio.create_task），不阻塞触发 API。
失败自动重试（MAX_RETRIES=3），超过转 failed。

调用 executor 的逻辑与 tests/api 平行（不依赖其私有函数），复用相同的端点契约。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

import app.core.db as db
from app.core.settings import settings
from app.common import evalset
from app.dataset import repo as dataset_repo
from app.dataset import revision
from app.benchmark import repo as bench_repo

logger = logging.getLogger("evolution.benchmark.runner")

# executor 调用配置（与 tests/api 对齐）
_EXEC_TIMEOUT = 30.0
_POLL_TIMEOUT = 600.0   # 单 case 10 分钟
_POLL_INTERVAL = 5.0


def _executor_url(path: str) -> str:
    return f"{settings.executor_url.rstrip('/')}{path}"


# ── 触发 ────────────────────────────────────────────────────


def trigger_run(
    *,
    versions: list[int] | None = None,
    case_ids: list[str] | None = None,
) -> str:
    """触发一个 benchmark 批次（同步建表，异步执行）。返回 batch_id。

    Args:
        versions: 要跑的版本号列表；None=当前 production 版本
        case_ids: 要跑的 case；None=golden 全 case
    """
    # 默认：当前 production 版本
    if versions is None:
        prod = _get_production_version()
        versions = [prod] if prod else []
    if not versions:
        raise ValueError("无可执行的 harness 版本（无 production 快照）")

    # 默认：golden 全 case
    if case_ids is None:
        case_ids = dataset_repo.get_golden_case_ids()
    if not case_ids:
        raise ValueError("golden 集为空，无 case 可跑")

    # golden revision（锁定值或实时计算）
    golden_revision = dataset_repo.get_golden_revision() or revision.compute_golden_revision()

    # 校验 golden 未被篡改
    locked = dataset_repo.get_golden_revision()
    if locked and not revision.verify_golden_intact(locked):
        logger.warning("golden 内容与锁定 revision 不一致（可能被篡改），仍用锁定值跑")

    batch_id = bench_repo.create_batch(
        case_ids=case_ids,
        versions=versions,
        golden_revision=golden_revision,
    )

    # 后台异步执行（不阻塞）
    asyncio.create_task(_run_batch_async(batch_id))
    logger.info("benchmark 批次 %s 已触发，后台执行", batch_id)
    return batch_id


def trigger_golden_upgrade_rerun(k: int = 3) -> str:
    """golden 升级后重跑最近 K 个版本（D8/D18/D20）。"""
    versions = bench_repo.get_recent_versions(k)
    if not versions:
        raise ValueError("无可用快照版本")
    return trigger_run(versions=versions)


# ── 后台执行 ────────────────────────────────────────────────


async def _run_batch_async(batch_id: str) -> None:
    """后台跑一个批次的所有 pending 行。"""
    await asyncio.to_thread(_run_batch_sync, batch_id)


def _run_batch_sync(batch_id: str) -> None:
    """同步执行批次：逐个跑 pending 行。"""
    logger.info("开始执行 benchmark 批次 %s", batch_id)
    while True:
        pending = bench_repo.get_pending(limit=1)
        if not pending:
            break
        row = pending[0]
        try:
            _execute_one(row)
        except Exception as exc:
            logger.exception("benchmark 行 %d 执行异常", row["id"])
            bench_repo.mark_failed(row["id"], str(exc))
    logger.info("benchmark 批次 %s 执行完毕", batch_id)


def _execute_one(row: dict[str, Any]) -> None:
    """执行单行：调 executor → 轮询 → 评估 → 写结果。"""
    run_id = row["id"]
    case_id = row["case_id"]
    version = row["harness_version"]

    bench_repo.mark_running(run_id)
    logger.info("benchmark [%d] case=%s version=%s", run_id, case_id, version)

    # 1. 取 demand_md + 版本快照
    demand_md = evalset.load_case_demand(case_id, layer="golden")
    snapshot = _get_snapshot(version)
    if snapshot is None:
        raise RuntimeError(f"harness v{version} 快照不存在")

    # 2. 调 executor
    task_id = _trigger_executor(demand_md, snapshot)

    # 3. 轮询完成
    trace_id = _poll_until_done(task_id, run_id)
    if not trace_id:
        raise RuntimeError(f"executor task {task_id} 无 trace_id")

    bench_repo.set_trace(run_id, trace_id)

    # 4. 评估（等 trace 摄入完成后再评）
    scores = _evaluate(trace_id)
    bench_repo.set_result(
        run_id,
        eval_id=scores.get("eval_id"),
        scores_json=json.dumps(scores, ensure_ascii=False) if scores else None,
    )
    logger.info("benchmark [%d] 完成: case=%s v=%s score=%s",
                run_id, case_id, version,
                scores.get("content_overall") if scores else "N/A")


# ── executor 调用（与 tests/api 平行，复用端点契约）─────────


def _get_production_version() -> int | None:
    """当前 production 快照版本号。"""
    row = db.query_one(
        "SELECT version FROM harness_snapshots WHERE status='production' AND config_json IS NOT NULL"
    )
    return row["version"] if row else None


def _get_snapshot(version: int) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM harness_snapshots WHERE version=? AND config_json IS NOT NULL",
        (version,),
    )


def _trigger_executor(demand_md: str, snapshot: dict[str, Any]) -> str:
    """调 executor /internal/ab/run，返回 task_id。"""
    config = json.loads(snapshot["config_json"]) if isinstance(snapshot["config_json"], str) else snapshot["config_json"]
    payload = {
        "config": config,
        "demand_md": demand_md,
        "baseline": False,
        "source_commit": snapshot["source_commit"],
    }
    resp = httpx.post(_executor_url("/internal/ab/run"), json=payload, timeout=_EXEC_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["task_id"]


def _poll_until_done(task_id: str, run_id: int) -> str | None:
    """轮询 executor task 直到完成，返回 trace_id。"""
    deadline = time.time() + _POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL)
        try:
            resp = httpx.get(_executor_url(f"/internal/ab/status/{task_id}"), timeout=10.0)
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        data = resp.json()
        trace_ids = data.get("trace_ids", [])
        status = data.get("status", "")

        if status == "done":
            return trace_ids[0] if trace_ids else None
        if status == "failed":
            raise RuntimeError(f"executor task failed: {data.get('error', 'unknown')}")
        if status == "cancelled":
            raise RuntimeError("executor task cancelled")
        # running：继续等

    raise RuntimeError(f"轮询超时（{_POLL_TIMEOUT}s 无结果）")


def _evaluate(trace_id: str) -> dict[str, Any]:
    """调 eval_agent/scoring 评估，返回分数摘要。

    等 trace 摄入完成（runs 表有该 trace）后再评。
    """
    # 等 trace 入库（executor done 后 ingestion 异步拉取，可能稍慢）
    for _ in range(20):  # 最多等 60s
        row = db.query_one("SELECT status FROM runs WHERE trace_id=?", (trace_id,))
        if row and row["status"] in ("completed", "failed"):
            break
        time.sleep(3.0)

    from app.eval_agent import scoring
    result = scoring.evaluate_trace(trace_id)
    if result is None:
        return {"eval_id": None, "content_overall": None, "skipped": True}

    # 提取摘要
    content = result.get("content", {})
    overall = float(content.get("overall", 0)) if not content.get("skipped") else None

    # 查 eval session
    eval_row = db.query_one(
        "SELECT eval_id FROM evaluation_sessions WHERE trace_id=? AND status='done' ORDER BY updated_at DESC LIMIT 1",
        (trace_id,),
    )
    return {
        "eval_id": eval_row["eval_id"] if eval_row else None,
        "content_overall": overall,
        "content_scores": content.get("scores", {}),
        "is_badcase": result.get("badcase", {}).get("is_badcase", False),
    }


__all__ = [
    "trigger_run",
    "trigger_golden_upgrade_rerun",
]
