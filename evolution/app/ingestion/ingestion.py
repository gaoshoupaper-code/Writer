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

import app.core.db as db
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
    # HITL：状态变迁即推送（awaiting_input / running / 终态）。
    # running 是 resume 后的状态变迁（awaiting_input→running），也需摄入同步。
    status: Literal["running", "awaiting_input", "completed", "failed", "cancelled"] = "completed"


def _fetch_trace_content(trace_id: str, since_seq: int = 0) -> tuple[list, str | None] | None:
    """从执行端 HTTP 拉取 trace 内容（run 摘要 + 事件列表）。

    since_seq（D8 增量）：只拉 sequence > since_seq 的事件。首次摄入传 0（全量）。
    执行端 GET /internal/traces/{trace_id}?since_seq=N 支持。

    Returns:
        (events, workspace_id_hint) 或 None（拉取失败/未找到）。
    """
    import httpx
    from contracts.trace import TraceLogEvent

    url = f"{settings.executor_url}/internal/traces/{trace_id}"
    params = {"since_seq": since_seq} if since_seq > 0 else None
    try:
        resp = httpx.get(url, timeout=_FETCH_TIMEOUT, params=params)
        if resp.status_code == 404:
            logger.warning("拉取 trace %s：执行端索引未命中（可能进程重启）", trace_id)
            return None
        resp.raise_for_status()
        data = resp.json()
        events = [TraceLogEvent.model_validate(e) for e in data.get("events", [])]
        workspace_hint = data.get("run", {}).get("workspace_id")
        return events, workspace_hint
    except Exception as exc:
        logger.warning("拉取 trace %s 失败：%s", exc)
        return None


def _load_prior_events(trace_id: str) -> tuple[list, int]:
    """读取本地已入库的事件（增量合并用，D8）+ 当前高水位。

    Returns:
        (prior_events, ingested_seq)。trace 不在库时返回 ([], 0)。
    """
    import json
    from contracts.trace import TraceLogEvent

    row = db.query_one("SELECT ingested_seq FROM runs WHERE trace_id = ?", (trace_id,))
    if row is None:
        return [], 0
    seq = row["ingested_seq"] or 0
    if seq == 0:
        return [], 0
    rows = db.query_all(
        "SELECT payload_json FROM event_payloads WHERE trace_id = ? ORDER BY sequence",
        (trace_id,),
    )
    prior = [TraceLogEvent.model_validate(json.loads(r["payload_json"])) for r in rows]
    return prior, seq


async def _ingest_async(trace_id: str) -> None:
    """在线程池中拉取 trace 内容并摄入（HTTP + 投影 + 写库，避免阻塞事件循环）。

    增量摄入（D8）：读本地高水位 ingested_seq → 只拉增量事件 → 合并旧事件全量投影。
    摄入完成后：
    - 旧 LLM-judge：已解除（不再对单条 trace 实时评估）
    - 新双层评估：仅终态触发（completed/cancelled/failed），awaiting_input/running 跳过
    """
    prior_events, since_seq = await asyncio.to_thread(_load_prior_events, trace_id)
    fetched = await asyncio.to_thread(_fetch_trace_content, trace_id, since_seq)
    if fetched is None:
        return
    events, workspace_hint = fetched
    # 增量场景：本次无新事件（since_seq 已是最新）。
    # 仍可能是状态变迁通知（如 awaiting_input→running，resume 不产生事件只改 index），
    # 故不直接 return：用执行端 run 摘要的 status 覆盖本地，保持状态最终一致。
    if since_seq > 0 and not events:
        await asyncio.to_thread(_sync_status_only, trace_id)
        return
    tid = await asyncio.to_thread(importer.ingest_events, events, workspace_hint, None, prior_events)
    if tid is None:
        return
    # 旧 LLM-judge 已解除（不再触发）
    # 新双层评估：仅终态触发（awaiting_input/running 不评估，防半成品污染）
    await asyncio.to_thread(_maybe_evaluate, tid, events, prior_events)


def _sync_status_only(trace_id: str) -> None:
    """无新事件时的状态同步：从执行端拉 run 摘要，覆盖本地 runs.status。

    典型场景：HITL resume（awaiting_input→running）不写 trace 事件，只改 index
    status。此时增量拉取无新事件，但本地 status 仍需同步，否则进化端永远停在旧状态。
    拉取失败静默（下次 scan/notify 会补）。
    """
    import httpx
    url = f"{settings.executor_url}/internal/traces/{trace_id}"
    try:
        resp = httpx.get(url, timeout=_FETCH_TIMEOUT)
        resp.raise_for_status()
        run = resp.json().get("run", {})
    except Exception as exc:
        logger.warning("状态同步拉取 %s 失败：%s", trace_id, exc)
        return
    status = run.get("status")
    if not status:
        return
    db.execute(
        "UPDATE runs SET status = ?, event_count = ? WHERE trace_id = ?",
        (status, run.get("event_count"), trace_id),
    )


def _maybe_judge(trace_id: str) -> None:
    """[已解除] 旧 LLM-judge（执行层评估）。

    HITL 改造（需求决策）：不再对单条 trace 实时触发旧 judge。此函数保留为空壳，
    避免其他调用点报错；调用链已在 _ingest_async 移除。双层评估（_maybe_evaluate）
    仍是活跃的评估入口。
    """
    return


def _maybe_evaluate(trace_id: str, events: list[Any], prior_events: list[Any] | None = None) -> None:
    """触发双层评估（仅终态）。

    HITL 改造：awaiting_input 是中间态，不评估（防半成品污染评估池）。只有终态
    （completed/cancelled/failed）才触发。
    防自指断路（D12）：optimization trace 跳过。
    合并 prior_events 一起判断（run_start 可能在旧事件里）。
    """
    try:
        from app.diagnosis.evaluation import evaluate_trace
        from app.core.llm import judge_enabled
        if not judge_enabled():
            return
        # 合并事件判断终态 + run_purpose
        merged = list(events) + (prior_events or [])
        if _is_awaiting(merged):
            logger.info("双层评估跳过 %s：awaiting_input 中间态", trace_id)
            return
        if not _is_terminal(merged):
            logger.info("双层评估跳过 %s：非终态", trace_id)
            return
        if _is_optimization_trace(merged):
            logger.info("双层评估跳过 %s：optimization trace（防自指断路）", trace_id)
            return
        evaluate_trace(trace_id)
    except Exception:
        logger.exception("双层评估触发失败 %s", trace_id)


def _is_awaiting(events: list[Any]) -> bool:
    """最后一条状态事件是否为 run_awaiting（当前处于 awaiting_input）。"""
    last = next((e for e in reversed(events) if e.type in ("run_awaiting", "run_end", "run_error", "run_cancelled")), None)
    return last is not None and last.type == "run_awaiting"


def _is_terminal(events: list[Any]) -> bool:
    """trace 是否已到终态（有 run_end/run_error/run_cancelled）。"""
    return any(e.type in ("run_end", "run_error", "run_cancelled") for e in events)


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
