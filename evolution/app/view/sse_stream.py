"""trace 实时增量 SSE 中继（D2/D4/D9）。

GET /api/traces/{trace_id}/stream：SSE 端点，逐事件推送 executor 原样 TraceLogEvent。

架构（D4 共享轮询注册表 + per-订阅者 Queue）：
  - _hub: dict[trace_id → HubEntry{task, since_seq, subscribers: set[Queue]}]
  - 每个 trace_id 一个共享后台 task，定时轮询 executor /internal/traces/{id}?since_seq=N
  - 增量事件 fan-out 给所有订阅者的 asyncio.Queue
  - 每个 SSE 请求开自己的 Queue，task 写入，请求结束从 subscribers 移除
  - 终态（completed/failed/cancelled）：发完最后一批 → event:end → 清理
  - 无订阅者：停 task 清理（避免空转）

设计依据：设计文档 D2/D4/D9 + 需求决策 6/7。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.core.settings import settings

logger = logging.getLogger("evolution.sse_stream")

router = APIRouter(tags=["sse"])

# 轮询间隔（秒）。2s 平衡实时感与 executor 压力。
_POLL_INTERVAL = 2.0
# 终态集合：拉回 run.status ∈ 这些 → 发完最后一批 → 停 task
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class _HubEntry:
    """单个 trace_id 的共享轮询入口。"""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.since_seq: int = 0
        self.subscribers: set[asyncio.Queue] = set()


# 进程级 hub：trace_id → HubEntry
_hub: dict[str, _HubEntry] = {}


@router.get("/traces/{trace_id}/stream")
async def stream_trace(trace_id: str) -> StreamingResponse:
    """SSE 流：逐事件推送 executor 原样 TraceLogEvent。

    消息格式（D9）：
      - data: {TraceLogEvent json}    ← 每条增量事件
      - event: snapshot / data: {...} ← 首条（可选，带 run summary 校准高水位）
      - event: end                     ← trace 终态，关闭流
      - event: error / data: {...}     ← executor 不可用（404 等）

    前端用 EventSource 消费，onmessage 收事件调 appendLiveTraceEvent 投影。
    """
    queue = await _subscribe(trace_id)
    return StreamingResponse(
        _event_generator(trace_id, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 不缓冲（防代理层攒包）
        },
    )


async def _event_generator(trace_id: str, queue: asyncio.Queue) -> Any:
    """SSE 事件生成器：从 queue 读消息，yield SSE 格式。"""
    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # 心跳：30s 无事件发 :keepalive，防代理掐断空闲连接
                yield ": keepalive\n\n"
                continue

            if msg is None:
                # 哨兵：流结束信号
                break

            msg_type = msg.get("_type", "data")
            if msg_type == "end":
                yield "event: end\ndata: {}\n\n"
                break
            if msg_type == "error":
                payload = json.dumps(msg.get("data", {}), ensure_ascii=False, default=str)
                yield f"event: error\ndata: {payload}\n\n"
                break
            if msg_type == "snapshot":
                payload = json.dumps(msg.get("data", {}), ensure_ascii=False, default=str)
                yield f"event: snapshot\ndata: {payload}\n\n"
                continue
            # 默认：逐事件推（D9）
            payload = json.dumps(msg.get("data", {}), ensure_ascii=False, default=str)
            yield f"data: {payload}\n\n"
    finally:
        _unsubscribe(trace_id, queue)


# ── hub 管理 ──


async def _subscribe(trace_id: str) -> asyncio.Queue:
    """订阅 trace 的增量流。首次订阅时启动共享轮询 task。"""
    if trace_id not in _hub:
        _hub[trace_id] = _HubEntry()
    entry = _hub[trace_id]
    queue: asyncio.Queue = asyncio.Queue()
    entry.subscribers.add(queue)

    # 首个订阅者启动 task（幂等：task 存在且未完成则不重启）
    if entry.task is None or entry.task.done():
        entry.task = asyncio.create_task(_poll_loop(trace_id, entry))

    return queue


def _unsubscribe(trace_id: str, queue: asyncio.Queue) -> None:
    """取消订阅。无订阅者时停 task 清理 hub 项。"""
    entry = _hub.get(trace_id)
    if entry is None:
        return
    entry.subscribers.discard(queue)
    if not entry.subscribers:
        # 最后一个订阅者离开 → 停 task
        if entry.task and not entry.task.done():
            entry.task.cancel()
        _hub.pop(trace_id, None)


async def _poll_loop(trace_id: str, entry: _HubEntry) -> None:
    """共享轮询 task：定时拉 executor 增量，fan-out 给所有订阅者。

    终态时发 end 哨兵 + 清理。executor 连续 404 时发 error 哨兵。
    """
    executor_url = getattr(settings, "executor_url", "").rstrip("/")
    consecutive_404 = 0

    while entry.subscribers:  # 无订阅者时自然退出（_unsubscribe 会 cancel）
        try:
            url = f"{executor_url}/internal/traces/{trace_id}?since_seq={entry.since_seq}"
            resp = await asyncio.to_thread(_fetch, url)
        except Exception:
            logger.debug("SSE 中继拉取失败 trace=%s", trace_id, exc_info=True)
            await _broadcast(entry, {"_type": "error", "data": {"reason": "fetch_failed"}})
            break

        if resp is None or resp.status_code == 404:
            consecutive_404 += 1
            if consecutive_404 >= 2:
                # 连续 404：executor 索引丢失（进程重启），通知前端降级
                await _broadcast(entry, {"_type": "error", "data": {"reason": "trace_not_found"}})
                break
            await asyncio.sleep(_POLL_INTERVAL)
            continue

        consecutive_404 = 0
        data = resp.json()
        run = data.get("run", {})
        events = data.get("events", [])

        # 首次或状态变化时推 snapshot（run summary，含 status/event_count）
        await _broadcast(entry, {"_type": "snapshot", "data": {
            "status": run.get("status"),
            "event_count": run.get("event_count", 0),
        }})

        # 逐事件推（D9：推 executor 原样 TraceLogEvent）
        for event in events:
            await _broadcast(entry, {"_type": "data", "data": event})
            seq = event.get("sequence", 0)
            if seq > entry.since_seq:
                entry.since_seq = seq

        # 终态：发完最后一批 → end 哨兵 → 退出
        if run.get("status") in _TERMINAL_STATUSES:
            await _broadcast(entry, {"_type": "end"})
            break

        await asyncio.sleep(_POLL_INTERVAL)

    # 退出清理：通知剩余订阅者关闭
    for q in list(entry.subscribers):
        await q.put({"_type": "end"})


async def _broadcast(entry: _HubEntry, msg: dict[str, Any]) -> None:
    """fan-out：把消息放进所有订阅者的 queue。"""
    for q in list(entry.subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            # 订阅者消费太慢，丢弃最旧消息（实时流，保最新）
            pass


def _fetch(url: str) -> httpx.Response | None:
    """同步 HTTP GET（在 to_thread 里跑）。"""
    try:
        resp = httpx.get(url, timeout=5.0)
        return resp
    except Exception:
        return None
