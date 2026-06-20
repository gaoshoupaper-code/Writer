"""trace 摄入路由：POST /ingestion/notify。

接收后端 recorder 在 complete_run/fail_run/cancel_run 后发出的完成通知，
读取 trace jsonl 并摄入。这是 monitoring 与后端的唯一耦合点。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

from app import importer
from app.settings import settings

router = APIRouter(tags=["ingestion"])
logger = logging.getLogger("monitoring.ingestion")


class NotifyBody(BaseModel):
    """后端完成通知的请求体。"""
    trace_id: str
    workspace_path: str | None = None   # 后端 workspace 根（可选，monitoring 优先用配置）
    thread_id: str | None = None
    trace_path: str                      # trace jsonl 路径（相对 workspace 根 或 绝对）
    status: Literal["completed", "failed", "cancelled"] = "completed"


def _resolve_trace_path(body: NotifyBody) -> Path:
    """把通知里的 trace_path 解析成绝对路径。

    后端传的可能是相对路径（相对 workspace 根）或绝对路径。
    """
    p = Path(body.trace_path)
    if p.is_absolute() and p.exists():
        return p
    # 相对路径：基于配置的 workspace 根
    return settings.backend_workspace_path / body.trace_path


async def _ingest_async(trace_path: Path, workspace_hint: str | None) -> None:
    """在线程池中执行摄入（文件 IO + 投影 + 写库，避免阻塞事件循环）。

    摄入完成后，若 trace 异常且 LLM 已配置，触发 LLM-judge（第二期）。
    """
    tid = await asyncio.to_thread(importer.ingest_trace, trace_path, workspace_hint)
    if tid is None:
        return
    # 第二期：异常 trace 触发 LLM-judge（judge 内部判断异常 + 幂等）
    await asyncio.to_thread(_maybe_judge, tid)


def _maybe_judge(trace_id: str) -> None:
    """若 LLM 启用且 trace 异常，触发评估。失败静默（judgment_runs 记录 error）。"""
    try:
        from app.judge import is_anomalous, judge_trace
        from app.llm import judge_enabled
        if not judge_enabled() or not is_anomalous(trace_id):
            return
        judge_trace(trace_id)
    except Exception:
        logger.exception("LLM-judge 触发失败 %s", trace_id)


@router.post("/ingestion/notify")
async def notify(body: NotifyBody, background_tasks: BackgroundTasks) -> dict[str, str]:
    """接收完成通知，异步摄入。

    返回 202 Accepted（摄入在后台进行，不阻塞后端的 complete_run 调用）。
    摄入失败只记日志——后端不依赖此返回（失败兜底扫描会补）。
    """
    trace_path = _resolve_trace_path(body)
    if not trace_path.exists():
        logger.warning("notify: trace 文件不存在 %s", trace_path)
        return {"status": "ignored", "reason": "trace file not found"}

    # workspace_id 提示：从 workspace_path 或 trace 路径推断
    workspace_hint = None
    if body.workspace_path:
        workspace_hint = Path(body.workspace_path).name
    else:
        # workspace/<工作区>/traces/... → 取倒数第三级
        parts = trace_path.parts
        workspace_hint = parts[-3] if len(parts) >= 3 else None

    background_tasks.add_task(_ingest_async, trace_path, workspace_hint)
    return {"status": "accepted", "trace_id": body.trace_id}
