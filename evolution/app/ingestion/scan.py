"""兜底扫描：定时调执行端列表 API，补摄入漏通知的 trace。

Phase 3 重构：不再扫执行端文件系统（glob workspace 目录），改调
GET /internal/traces 列表端点拿近期 trace 清单，对未摄入的逐个拉取。

设计：执行端通知可能丢失（网络/evolution 未启动），靠定时扫描保证最终一致。
判断"已摄入"：runs 表已有该 trace_id。
"""

from __future__ import annotations

import asyncio
import logging

import app.core.db as db
from app.core.settings import settings

logger = logging.getLogger("evolution.scan")

_SCAN_INTERVAL = 60.0  # 扫描间隔（秒）
_task: asyncio.Task | None = None


def start_scan_scheduler() -> None:
    """启动兜底扫描后台任务（幂等）。在 lifespan 启动时调用。"""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_scan_loop())


async def _scan_loop() -> None:
    """周期扫描：找漏通知的 trace 补摄入。"""
    # 启动后先扫一次（接住 evolution 重启期间漏的）
    await asyncio.to_thread(_scan_once)
    while True:
        await asyncio.sleep(_SCAN_INTERVAL)
        try:
            await asyncio.to_thread(_scan_once)
        except Exception:
            logger.exception("兜底扫描异常")


def _scan_once() -> int:
    """扫描一次，返回本次补摄入的数量。

    庚方案（D9）：调执行端 GET /internal/traces 拿近期 trace 清单（带 status），
    逐条对比 evolution runs 表的 status：
    - evolution 没有 → 新 trace，拉取摄入
    - status 不一致 → 状态变迁（如 awaiting_input→completed），重拉摄入
    - status 一致 → 跳过

    EVD-009 根因修复：status 一致（两端都 running）不再无脑跳过——补三方对账：
    若关联的 manual_tests/eval/evolve session 已终态，而 trace 两端都还 running，
    说明是强杀/超时产生的孤儿（task/test/trace 三状态分裂），必须重拉摄入让 receipt
    按真实事件重新判定，否则该 trace 永久伪装为活跃。
    """
    import httpx

    url = f"{settings.executor_url}/internal/traces"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        recent_traces = data.get("traces", [])
    except Exception as exc:
        logger.warning("兜底扫描：拉取 trace 列表失败：%s", exc)
        return 0

    if not recent_traces:
        return 0

    # 已摄入的 trace_id → status 映射（用于变迁检测）
    ingested_rows = db.query_all("SELECT trace_id, status FROM runs")
    ingested_status = {r["trace_id"]: r["status"] for r in ingested_rows}
    count = 0
    for item in recent_traces:
        trace_id = item.get("trace_id", "")
        if not trace_id:
            continue
        executor_status = item.get("status", "")
        local_status = ingested_status.get(trace_id)
        # 跳过条件：已摄入且 status 一致（无变迁）—— 但 EVD-009 还需三方对账。
        if local_status is not None and local_status == executor_status:
            # trace 稳定性重构：interrupted 是本地判定，executor 不知，不得被覆盖。
            if local_status == "interrupted":
                continue
            # 两端都 running 时，检查关联业务对象是否已终态（孤儿检测，EVD-009）。
            if local_status in ("running", "awaiting_input", "cancelling") and _associated_business_terminal(trace_id):
                logger.info(
                    "孤儿 trace 检测: %s 两端 %s 但关联业务对象已终态，强制重拉摄入",
                    trace_id, local_status,
                )
            else:
                continue
        # interrupted 本地态不被 executor 覆盖（仅 UI 手动收敛）。
        if local_status == "interrupted" and executor_status == "running":
            continue
        # 新 trace 或 status 变迁或孤儿 → 拉取摄入
        tid = _fetch_and_ingest(trace_id, item.get("workspace_id"))
        if tid:
            count += 1
            logger.info("兜底摄入: %s (变更: %s→%s)", tid, local_status, executor_status)
    return count


# 业务终态集合（与各 session 表的终态对齐）。
_BUSINESS_TERMINAL_STATUSES = {
    "done", "failed", "cancelled", "cancel_timeout", "interrupted",
    "completed",  # evaluation_sessions 用 completed
    "published", "discarded",  # evolve_sessions 终态
}


def _associated_business_terminal(trace_id: str) -> bool:
    """EVD-009 三方对账：检查 trace 关联的业务对象（test/eval/evolve）是否已终态。

    若关联对象已终态而 trace 仍 running，说明发生了 task/test/trace 三状态分裂
    （强杀/超时只改了一侧），该 trace 是孤儿——调用方应强制重拉摄入。
    任一关联表命中终态即返回 True；无关联或关联仍在跑返回 False。
    """
    from contracts.cancel_state import is_terminal

    # manual_tests.trace_id（单次测试被测对象）
    row = db.query_one("SELECT status FROM manual_tests WHERE trace_id=?", (trace_id,))
    if row is not None:
        if is_terminal(row["status"]) or row["status"] in _BUSINESS_TERMINAL_STATUSES:
            return True
    # evolve/eval session 的 self_trace_id（自观测录像）
    for table, col in (("evolve_sessions", "self_trace_id"), ("evaluation_sessions", "self_trace_id")):
        try:
            row = db.query_one(f"SELECT status FROM {table} WHERE {col}=?", (trace_id,))
        except Exception:
            continue
        if row is not None and row["status"] in _BUSINESS_TERMINAL_STATUSES:
            return True
    return False


def _fetch_and_ingest(trace_id: str, workspace_hint: str | None) -> str | None:
    """拉取单个 trace 内容并摄入（兜底扫描专用）。"""
    from app.ingestion.ingestion import _fetch_trace_content
    from app.ingestion import importer

    fetched = _fetch_trace_content(trace_id)
    if fetched is None:
        return None
    events, run_summary, payload_values = fetched
    # 优先用列表端点返回的 workspace_id；run summary 保持 V2 manifest 与完整性语义。
    return importer.ingest_events(
        events, workspace_hint or run_summary.workspace_id, run_status_hint=run_summary.status,
        run_summary_hint=run_summary, payload_values=payload_values,
    )
