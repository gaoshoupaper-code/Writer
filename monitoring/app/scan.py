"""兜底扫描：定时扫 backend_workspace，补摄入漏通知的 trace。

设计：后端通知可能丢失（网络/monitoring 未启动），靠定时扫描保证最终一致。
判断"已摄入"：runs 表已有该 trace_id。
trace_id 从文件名提取（trace-<uuid>.jsonl）。
"""

from __future__ import annotations

import asyncio
import logging
import glob
from pathlib import Path

import app.db as db
from app import importer
from app.settings import settings

logger = logging.getLogger("monitoring.scan")

_SCAN_INTERVAL = 60.0  # 扫描间隔（秒）
_task: asyncio.Task | None = None


def start_scan_scheduler() -> None:
    """启动兜底扫描后台任务（幂等）。在 lifespan 启动时调用。"""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_scan_loop())


async def _scan_loop() -> None:
    """周期扫描：找漏通知的 trace 补摄入。"""
    # 启动后先扫一次（接住 monitoring 重启期间漏的）
    await asyncio.to_thread(_scan_once)
    while True:
        await asyncio.sleep(_SCAN_INTERVAL)
        try:
            await asyncio.to_thread(_scan_once)
        except Exception:
            logger.exception("兜底扫描异常")


def _scan_once() -> int:
    """扫描一次，返回本次补摄入的数量。"""
    ws = settings.backend_workspace_path
    if not ws.exists():
        return 0
    # 所有 trace jsonl，兼容两种 workspace 结构：
    #   旧（单层）：workspace/<工作区>/traces/<时间戳>/trace-<uuid>.jsonl
    #   新（Phase 2 多用户分桶）：workspace/<user_id>/<workspace_id>/traces/<时间戳>/trace-<uuid>.jsonl
    # 用 ** 递归匹配 traces 前的任意层级。
    pattern = str(ws / "**" / "traces" / "*" / "trace-*.jsonl")
    trace_files = glob.glob(pattern, recursive=True)
    if not trace_files:
        return 0

    # 已摄入的 trace_id 集合
    ingested = {r["trace_id"] for r in db.query_all("SELECT trace_id FROM runs")}
    count = 0
    for tp_str in trace_files:
        tp = Path(tp_str)
        trace_id = tp.stem  # trace-<uuid>
        if trace_id in ingested:
            continue
        # workspace_id 提示：traces 的上一级目录名。
        # 新结构里 traces 上一级是 workspace_id，再上一级是 user_id（importer 从 run_start 提取）。
        parts = tp.parts
        workspace_hint = None
        if "traces" in parts:
            idx = parts.index("traces")
            if idx >= 1:
                workspace_hint = parts[idx - 1]
        try:
            tid = importer.ingest_trace(tp, workspace_hint)
            if tid:
                count += 1
                logger.info("兜底摄入: %s", tid)
                # 第二期：异常 trace 触发 LLM-judge
                _maybe_judge_scan(tid)
        except Exception:
            logger.exception("兜底摄入失败: %s", tp)
    return count


def _maybe_judge_scan(trace_id: str) -> None:
    """兜底摄入后，若 LLM 启用且 trace 异常，触发评估。"""
    try:
        from app.judge import is_anomalous, judge_trace
        from app.llm import judge_enabled
        if not judge_enabled() or not is_anomalous(trace_id):
            return
        judge_trace(trace_id)
    except Exception:
        logger.exception("LLM-judge 触发失败(兜底) %s", trace_id)
