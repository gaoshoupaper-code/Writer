"""trace 实时增量 SSE 中继（D2/D4/D9 + Phase 2 T5 双数据源）。

GET /api/traces/{trace_id}/stream：SSE 端点。

双数据源（Phase 2 T5 统一信封按 source 分流）：
  - evolution 源：trace_id 在 recorder 活跃列表 → 后端投影 + diff → 推 node patch
  - executor 源：trace_id 不在 recorder → 轮询 executor /internal/traces → 推原始 event

消息格式（Phase 2 T5 统一信封）：
  - data: {"_type":"data","source":"evolution","data":{"appended":[...],"updated":[...]}}
  - data: {"_type":"data","source":"executor","data":{TraceLogEvent json}}
  - data: {"_type":"snapshot","source":"evolution","data":[nodes]}
  - data: {"_type":"snapshot","source":"executor","data":{run summary}}
  - event: end / event: error

前端按 source 分流：evolution 源收 patch 做 append/update，executor 源收 event 投影。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core.settings import settings

logger = logging.getLogger("evolution.sse_stream")

router = APIRouter(tags=["sse"])

# 轮询间隔（秒）。
# - executor 源：2s（平衡实时感与 executor 压力）
_POLL_INTERVAL = 2.0
# - evolution 源：0.5s（后端投影 + diff，比 executor 源更轻量，可更快）
_EVO_POLL_INTERVAL = 0.5
# 终态集合：拉回 run.status ∈ 这些 → 发完最后一批 → 停 task
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class _HubEntry:
    """单个 trace_id 的共享轮询入口。"""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.since_seq: int = 0
        self.subscribers: set[asyncio.Queue] = set()
        self.source: str = "executor"  # "evolution" | "executor"


# 进程级 hub：trace_id → HubEntry
_hub: dict[str, _HubEntry] = {}


@router.get("/traces/{trace_id}/stream")
async def stream_trace(trace_id: str, request: Request) -> StreamingResponse:
    """SSE 流：按 trace 来源（evolution / executor）分流推送。"""
    queue = await _subscribe(trace_id, request)
    return StreamingResponse(
        _event_generator(trace_id, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 不缓冲（防代理层攒包）
        },
    )


async def _event_generator(trace_id: str, queue: asyncio.Queue) -> Any:
    """SSE 事件生成器：从 queue 读消息，yield SSE 格式。

    Phase 2 T5 统一信封：消息含 source 字段，前端按 source 分流处理。
    """
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
                # snapshot：前端全量替换 nodes（终态对齐 T9）
                payload = json.dumps(msg, ensure_ascii=False, default=str)
                yield f"data: {payload}\n\n"
                continue
            # 默认 data：逐消息推（含 source 字段）
            payload = json.dumps(msg, ensure_ascii=False, default=str)
            yield f"data: {payload}\n\n"
    finally:
        _unsubscribe(trace_id, queue)


# ── hub 管理 ──


async def _subscribe(trace_id: str, request: Request) -> asyncio.Queue:
    """订阅 trace 的增量流。首次订阅时启动共享轮询 task。

    自动判断数据源：trace_id 在 recorder 活跃列表 → evolution 源，否则 executor 源。
    """
    if trace_id not in _hub:
        _hub[trace_id] = _HubEntry()
    entry = _hub[trace_id]

    # 判断数据源（每个 trace 只判断一次，source 固定后不变）
    if entry.task is None or entry.task.done():
        recorder = getattr(request.app.state, "trace_recorder", None)
        if recorder is not None and not recorder.is_terminal(trace_id):
            entry.source = "evolution"
        else:
            entry.source = "executor"

    queue: asyncio.Queue = asyncio.Queue()
    entry.subscribers.add(queue)

    # 首个订阅者启动 task（幂等：task 存在且未完成则不重启）
    if entry.task is None or entry.task.done():
        if entry.source == "evolution":
            entry.task = asyncio.create_task(_evo_poll_loop(trace_id, entry, request))
        else:
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
    """executor 源：定时拉 executor 增量，fan-out 给所有订阅者。

    Phase 2 T5：消息带 source="executor"，前端按 source 分流。
    """
    executor_url = getattr(settings, "executor_url", "").rstrip("/")
    consecutive_404 = 0

    while entry.subscribers:
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
                await _broadcast(entry, {"_type": "error", "data": {"reason": "trace_not_found"}})
                break
            await asyncio.sleep(_POLL_INTERVAL)
            continue

        consecutive_404 = 0
        data = resp.json()
        run = data.get("run", {})
        events = data.get("events", [])

        # snapshot：run summary（前端用于校准状态/事件数）
        await _broadcast(entry, {
            "_type": "snapshot", "source": "executor",
            "data": {"status": run.get("status"), "event_count": run.get("event_count", 0)},
        })

        # 逐事件推（executor 原样 TraceLogEvent）
        for event in events:
            await _broadcast(entry, {"_type": "data", "source": "executor", "data": event})
            seq = event.get("sequence", 0)
            if seq > entry.since_seq:
                entry.since_seq = seq

        if run.get("status") in _TERMINAL_STATUSES:
            await _broadcast(entry, {"_type": "end"})
            break

        await asyncio.sleep(_POLL_INTERVAL)

    for q in list(entry.subscribers):
        await q.put({"_type": "end"})


async def _evo_poll_loop(trace_id: str, entry: _HubEntry, request: Request) -> None:
    """evolution 源：从 recorder 投影 + diff，fan-out node patch 给订阅者。

    Phase 2 T4 路线 Y + T5：每 _EVO_POLL_INTERVAL 秒全量投影一次，
    diff 出变更的 node 推给前端。终态时推全量 snapshot + end。
    """
    recorder = getattr(request.app.state, "trace_recorder", None)

    while entry.subscribers:
        if recorder is None:
            await _broadcast(entry, {"_type": "error", "data": {"reason": "recorder_unavailable"}})
            break

        # 全量投影 + diff
        try:
            patch = await asyncio.to_thread(recorder.project_and_diff, trace_id)
        except Exception:
            logger.debug("evolution SSE 投影失败 trace=%s", trace_id, exc_info=True)
            patch = None

        if patch is not None:
            # 有变更才推（append 或 updated 非空）
            if patch.get("appended") or patch.get("updated"):
                await _broadcast(entry, {
                    "_type": "data", "source": "evolution",
                    "data": {
                        "appended": [_node_to_dict(n) for n in patch["appended"]],
                        "updated": [_node_to_dict(n) for n in patch["updated"]],
                    },
                })

        # 终态判断：trace 不再活跃（已从 recorder 队列移除）
        if recorder.is_terminal(trace_id):
            # 推全量 snapshot 强制对齐（T9）
            try:
                full_nodes = await asyncio.to_thread(recorder.project_full_nodes, trace_id)
            except Exception:
                full_nodes = None
            if full_nodes is not None:
                await _broadcast(entry, {
                    "_type": "snapshot", "source": "evolution",
                    "data": [_node_to_dict(n) for n in full_nodes],
                })
            await _broadcast(entry, {"_type": "end"})
            break

        await asyncio.sleep(_EVO_POLL_INTERVAL)

    for q in list(entry.subscribers):
        await q.put({"_type": "end"})


def _node_to_dict(node: Any) -> dict[str, Any]:
    """TraceNode → 可 JSON 序列化的 dict（含 raw_event_ids 等 TraceNode 全字段）。"""
    return node.model_dump(exclude_none=True)


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
