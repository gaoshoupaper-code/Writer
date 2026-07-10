"""trace 摄入路由：POST /ingestion/notify。

接收执行端 recorder 在 complete_run/fail_run/cancel_run 后发出的完成通知，
通过 HTTP 从执行端拉取 trace 内容并摄入。这是 evolution 与执行端的唯一数据入口。

Phase 3 重构：不再读执行端文件系统，改调 GET /internal/traces/{trace_id} 拉内容。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
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
    # 手动测试兜底（D-Q14）：任务在产出 trace 前就失败时，executor 带 task_id 通知。
    # 此时 trace_id 为空，按 task_id 反查测试记录标 failed。
    task_id: str | None = None
    error: str | None = None


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
    # 评估已从摄入链路解耦（决策 S6）：不再摄入时自动评估，
    # 评估统一由 eval_agent 手动触发（POST /eval-agent/start）。
    # 手动测试状态同步：按 trace_id 同步 manual_tests 终态（D-Q3）
    run_row = db.query_one("SELECT status FROM runs WHERE trace_id=?", (tid,))
    if run_row:
        await asyncio.to_thread(_sync_manual_test_status, tid, run_row["status"])


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

    评估已从摄入链路彻底解耦（决策 S6）：不再摄入时触发任何评估，
    评估统一由 eval_agent 手动触发（POST /eval-agent/start）。
    此空壳保留避免其他调用点报错。
    """
    return


@router.post("/ingestion/notify")
async def notify(body: NotifyBody, background_tasks: BackgroundTasks) -> dict[str, str]:
    """接收完成通知，异步拉取 trace 内容并摄入。

    返回 202 Accepted（摄入在后台进行，不阻塞执行端的 complete_run 调用）。
    摄入失败只记日志——执行端不依赖此返回（失败兜底扫描会补）。

    手动测试兜底（D-Q14）：trace_id 为空 + task_id 有值 = 任务在产出 trace 前失败，
    按 task_id 反查测试记录标 failed（不走摄入链路）。
    """
    # 手动测试兜底：task 失败无 trace
    if not body.trace_id and body.task_id:
        _mark_test_failed_by_task(body.task_id, body.error or "executor task failed")
        return {"status": "accepted", "task_id": body.task_id}
    background_tasks.add_task(_ingest_async, body.trace_id)
    return {"status": "accepted", "trace_id": body.trace_id}


@router.get("/ingestion/active-key")
def get_active_llm_key() -> dict[str, str]:
    """executor 拉取激活 LLM 配置明文（内网专用，X-Notify-Token 鉴权）。

    executor 的 build_writer_model 优先从这里取 (api_key, base_url, model)，
    替代空的 PLATFORM_API_KEY/OPENAI_API_KEY 环境变量。

    鉴权：挂 /api/ingestion/ 前缀 → SSO 放行 → NotifyTokenMiddleware 校验
    X-Notify-Token（与 notify 端点同保护级）。明文 key 只在内网传给 executor，
    不暴露给桌面端。

    Returns:
        {api_key, base_url, model}；未配置激活配置返回 404（executor 据此降级）。
    """
    config = None
    try:
        config = db.LlmConfigsRepository.get_active()
    except Exception:
        # 表未建/迁移中（init_db 尚未跑完）→ 当作未配置，让 executor 走环境变量降级
        logger.warning("读取激活 LLM 配置异常（表可能未迁移），返回 404", exc_info=True)
    if config is None:
        raise HTTPException(status_code=404, detail="未配置激活的 LLM 配置")
    api_key, base_url, model = config
    return {"api_key": api_key, "base_url": base_url, "model": model}


def _mark_test_failed_by_task(task_id: str, error: str) -> None:
    """按 task_id 把仍在 running 的测试记录标 failed（D-Q14 兜底）。"""
    try:
        from app.tests import repo as test_repo

        row = test_repo.find_pending_by_task_id(task_id)
        if row:
            test_repo.mark_failed(row["test_id"], error)
            logger.info("测试记录 %s 兜底标 failed（task=%s）", row["test_id"], task_id)
    except Exception:
        logger.exception("兜底标记测试 failed 失败 task=%s", task_id)


def _sync_manual_test_status(trace_id: str, run_status: str) -> None:
    """trace 摄入完成后，按 trace_id 同步关联的手动测试记录状态（D-Q3）。

    completed → done；failed/cancelled → failed。
    """
    try:
        from app.tests import repo as test_repo

        row = test_repo.find_by_trace_id(trace_id)
        if not row:
            return  # 非 manual_test 触发的 trace，跳过
        if row["status"] in ("done", "failed"):
            return  # 已终结，不重复更新
        if run_status == "completed":
            test_repo.mark_done(row["test_id"], trace_id)
        elif run_status in ("failed", "cancelled"):
            test_repo.mark_failed(row["test_id"], f"trace {run_status}", trace_id=trace_id)
    except Exception:
        logger.exception("同步手动测试状态失败 trace=%s", trace_id)
