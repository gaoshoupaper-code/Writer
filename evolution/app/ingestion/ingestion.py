"""trace 摄入路由：POST /ingestion/notify。

接收执行端 recorder 在 complete_run/fail_run/cancel_run 后发出的完成通知，
通过 HTTP 从执行端拉取 trace 内容并摄入。这是 evolution 与执行端的唯一数据入口。

Phase 3 重构：不再读执行端文件系统，改调 GET /internal/traces/{trace_id} 拉内容。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from app.ingestion import importer
from app.core.settings import settings

router = APIRouter(tags=["ingestion"])
logger = logging.getLogger("evolution.ingestion")

# HTTP 拉取超时：trace 内容可能较大（大 trace 上 MB），给充足余量。
_FETCH_TIMEOUT = 30.0


class NotifyBody(BaseModel):
    """执行端完成通知的请求体。"""
    trace_id: str
    workspace_path: str | None = None
    thread_id: str | None = None
    # Phase 3：trace_path 字段已废弃（解耦后 evolution 不读文件）。
    # 执行端 T3.4 后不再发送；收到时忽略，不影响摄入。
    trace_path: str = ""
    status: Literal["completed", "failed", "cancelled"] = "completed"


def _fetch_trace_content(trace_id: str) -> tuple[list, str | None] | None:
    """从执行端 HTTP 拉取 trace 内容（run 摘要 + 事件列表）。

    Returns:
        (events, workspace_id_hint) 或 None（拉取失败/未找到）。
    """
    import httpx
    from contracts.trace import TraceLogEvent

    url = f"{settings.executor_url}/internal/traces/{trace_id}"
    try:
        resp = httpx.get(url, timeout=_FETCH_TIMEOUT)
        if resp.status_code == 404:
            logger.warning("拉取 trace %s：执行端索引未命中（可能进程重启）", trace_id)
            return None
        resp.raise_for_status()
        data = resp.json()
        events = [TraceLogEvent.model_validate(e) for e in data.get("events", [])]
        workspace_hint = data.get("run", {}).get("workspace_id")
        return events, workspace_hint
    except Exception as exc:
        logger.warning("拉取 trace %s 失败：%s", trace_id, exc)
        return None


async def _ingest_async(trace_id: str) -> None:
    """在线程池中拉取 trace 内容并摄入（HTTP + 投影 + 写库，避免阻塞事件循环）。

    摄入完成后：
    - 旧 LLM-judge（执行层泛维度评估）：仅异常 trace 触发（向后兼容）
    - 新双层评估（网文专业领域评估）：全量触发，但 run_purpose=optimization 的
      trace 跳过（防自指断路：优化回放的产出不进评估池，复用 recorder D12 预留）
    """
    fetched = await asyncio.to_thread(_fetch_trace_content, trace_id)
    if fetched is None:
        return
    events, workspace_hint = fetched
    tid = await asyncio.to_thread(importer.ingest_events, events, workspace_hint)
    if tid is None:
        return
    # 旧 LLM-judge：仅异常 trace（向后兼容，执行层评估仍有价值）
    await asyncio.to_thread(_maybe_judge, tid)
    # 新双层评估：全量触发（防自指断路：optimization trace 跳过）
    await asyncio.to_thread(_maybe_evaluate, tid, events)


def _maybe_judge(trace_id: str) -> None:
    """若 LLM 启用且 trace 异常，触发旧 LLM-judge（执行层评估）。失败静默。"""
    try:
        from app.diagnosis.judge import is_anomalous, judge_trace
        from app.core.llm import judge_enabled
        if not judge_enabled() or not is_anomalous(trace_id):
            return
        judge_trace(trace_id)
    except Exception:
        logger.exception("LLM-judge 触发失败 %s", trace_id)


def _maybe_evaluate(trace_id: str, events: list[Any]) -> None:
    """全量触发双层评估（T1.5）。

    防自指断路（D12）：从 run_start 事件取 run_purpose，
    optimization 的 trace 跳过评估（不进评估池，避免优化回放自评形成正反馈）。
    """
    try:
        from app.diagnosis.evaluation import evaluate_trace
        from app.core.llm import judge_enabled
        if not judge_enabled():
            return
        if _is_optimization_trace(events):
            logger.info("双层评估跳过 %s：optimization trace（防自指断路）", trace_id)
            return
        evaluate_trace(trace_id)
    except Exception:
        logger.exception("双层评估触发失败 %s", trace_id)


def _is_optimization_trace(events: list[Any]) -> bool:
    """判断 trace 是否为优化回放产出（run_purpose=optimization）。

    从 run_start 事件的 input.run_purpose 字段判断。recorder 已在 run_start 埋点（D12）。
    无法判断时返回 False（安全侧：宁可评估不跳过）。
    """
    run_start = next((e for e in events if e.type == "run_start"), None)
    if run_start is None or not isinstance(run_start.input, dict):
        return False
    return str(run_start.input.get("run_purpose", "")) == "optimization"


@router.post("/ingestion/notify")
async def notify(body: NotifyBody, background_tasks: BackgroundTasks) -> dict[str, str]:
    """接收完成通知，异步拉取 trace 内容并摄入。

    返回 202 Accepted（摄入在后台进行，不阻塞执行端的 complete_run 调用）。
    摄入失败只记日志——执行端不依赖此返回（失败兜底扫描会补）。
    """
    background_tasks.add_task(_ingest_async, body.trace_id)
    return {"status": "accepted", "trace_id": body.trace_id}
