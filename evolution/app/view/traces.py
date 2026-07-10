"""trace 查看路由：取代后端 GET /threads/{tid}/traces 系列。

维度：全局 / trace_id（不依赖 thread 鉴权，纯内部工具）。
详情通过重新投影 event_payloads 还原完整 TraceDetail（含 context/todos）。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import app.core.db as db
from app.ingestion import projector
from app.core.models import (
    TraceContextSegment,
    TraceLogEvent,
    TraceNode,
    TraceRunSummary,
    TraceTodoSnapshot,
)

router = APIRouter(tags=["traces"])


class TraceDetailLite(BaseModel):
    """详情接口轻量返回（去 events/context，前端按需懒加载）。

    events 和 context 是 trace 里最大的两块数据（上千事件 × 每条含完整 input/output，
    可达几十 MB）。详情接口只返回投影后的精简结果（nodes/todos），
    events 按 node.raw_event_ids 懒加载，context 按 anchor_id 懒加载。
    """

    run: TraceRunSummary
    nodes: list[TraceNode]
    todos: list[TraceTodoSnapshot]


class TraceListItem(BaseModel):
    """trace 列表项（runs 表行 + 命中规则数）。"""
    trace_id: str
    workspace_id: str
    thread_id: str | None
    session_name: str | None
    endpoint: str | None
    status: str
    started_at: str | None
    ended_at: str | None
    duration_ms: int | None
    event_count: int
    error: str | None
    flag_count: int = 0   # 命中规则数（标红数）
    owner_user_id: str = "unknown"   # 归属用户（Phase 3 D16）
    run_purpose: str = "user_generation"   # trace 来源（D2：区分执行端/进化端）


@router.get("/traces", response_model=list[TraceListItem])
def list_traces(
    workspace: str | None = Query(None, description="按 workspace_id 过滤"),
    thread_id: str | None = Query(None, description="按 thread_id 过滤"),
    status: str | None = Query(None, description="按 status 过滤"),
    owner: str | None = Query(None, description="按 owner_user_id 过滤（D16 防串户）"),
    run_purpose: str | None = Query(None, description="按 run_purpose 过滤（evolution_eval/evolution_evolve/user_generation）"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[TraceListItem]:
    """全局 trace 列表，按 started_at 倒序。"""
    where: list[str] = []
    params: list[Any] = []
    if workspace:
        where.append("r.workspace_id = ?")
        params.append(workspace)
    if thread_id:
        where.append("r.thread_id = ?")
        params.append(thread_id)
    if status:
        where.append("r.status = ?")
        params.append(status)
    if run_purpose:
        where.append("r.run_purpose = ?")
        params.append(run_purpose)
    if owner:
        where.append("r.owner_user_id = ?")
        params.append(owner)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db.query_all(
        f"""SELECT r.* FROM runs r {where_sql}
            ORDER BY r.started_at DESC LIMIT ? OFFSET ?""",
        tuple(params + [limit, offset]),
    )
    return [
        TraceListItem(
            trace_id=r["trace_id"], workspace_id=r["workspace_id"],
            thread_id=r["thread_id"], session_name=r["session_name"],
            endpoint=r["endpoint"], status=r["status"],
            started_at=r["started_at"], ended_at=r["ended_at"],
            duration_ms=r["duration_ms"], event_count=r["event_count"] or 0,
            error=r["error"], flag_count=0,
            owner_user_id=r.get("owner_user_id") or "unknown",
            run_purpose=r.get("run_purpose") or "user_generation",
        )
        for r in rows
    ]


@router.get("/traces/{trace_id}", response_model=TraceDetailLite)
def get_trace(trace_id: str) -> TraceDetailLite:
    """trace 详情（轻量）：run + nodes + todos。

    events 和 context 不再全量返回（它们是 trace 最大的两块数据）。
    前端打开抽屉时通过 /events 和 /context 懒加载接口按需拉取。

    nodes/todos 始终重新投影（增量重建已优化为 O(N)，且 nodes 表不存 raw_event_ids
    等投影字段，todos 无独立表——重新投影是唯一完整数据源）。
    """
    run_row = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run_row is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    run = _run_summary_from_row(run_row)
    events = _reconstruct_incremental_inputs(_load_events(trace_id))
    projection = projector.TraceProjector().project(run, events)
    return TraceDetailLite(
        run=run, nodes=projection.nodes, todos=projection.todos,
    )


@router.get("/traces/{trace_id}/events", response_model=list[TraceLogEvent])
def get_trace_events(
    trace_id: str,
    event_ids: str = Query(..., description="逗号分隔的 event_id 列表"),
) -> list[TraceLogEvent]:
    """按 event_id 批量拉取原始事件（抽屉懒加载用）。

    前端从 node.raw_event_ids 拿到事件 id 列表，调本接口批量拉取。
    返回事件含完整 input/output（增量 input 已重建）。
    """
    run_row = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run_row is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    id_list = [eid.strip() for eid in event_ids.split(",") if eid.strip()]
    if not id_list:
        return []

    # 从 DB 批量查 event_payloads
    placeholders = ",".join("?" * len(id_list))
    rows = db.query_all(
        f"SELECT payload_json FROM event_payloads WHERE trace_id=? AND event_id IN ({placeholders})",
        (trace_id, *id_list),
    )
    if not rows:
        return []

    events = [TraceLogEvent.model_validate(json.loads(r["payload_json"])) for r in rows]

    # 增量重建（只针对本次拉取的事件集——但如果需要完整重建，
    # 前端应拉全链事件。这里对拉取的 llm_start 做单条重建降级处理）
    has_incremental = any(
        e.type == "llm_start" and e.input_context_range is not None for e in events
    )
    if has_incremental:
        # 需要全链重建：加载该 trace 所有事件做批量重建，再筛出请求的
        all_events = _reconstruct_incremental_inputs(_load_events(trace_id))
        wanted = {eid for eid in id_list}
        return [e for e in all_events if e.event_id in wanted]
    return events


@router.get("/traces/{trace_id}/context", response_model=TraceContextSegment)
def get_trace_context(
    trace_id: str,
    anchor_id: str = Query(..., description="context segment 的 anchor_id"),
) -> TraceContextSegment:
    """按 anchor_id 拉取单个 context segment（抽屉懒加载用）。

    context 是 trace 里第二大的数据块（每条含完整 system prompt/消息体）。
    详情接口不返回 context 全量，前端打开抽屉时按 anchor 懒加载。
    """
    run_row = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run_row is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    # context 在投影时产出，没有单独的表——需要投影后按 anchor_id 筛选。
    # 对大 trace 这仍有开销，但只在用户主动打开抽屉时触发（非首屏）。
    events = _reconstruct_incremental_inputs(_load_events(trace_id))
    run = _run_summary_from_row(run_row)
    projection = projector.TraceProjector().project(run, events)
    for seg in projection.context:
        if seg.anchor_id == anchor_id:
            return seg
    raise HTTPException(status_code=404, detail="Context segment not found")


@router.delete("/traces/{trace_id}")
def delete_trace(trace_id: str) -> dict[str, str]:
    """删除 trace 的 evolution 记录（runs/nodes/events/flags 随级联删除）。"""
    cur = db.execute("DELETE FROM runs WHERE trace_id = ?", (trace_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"status": "ok", "deleted": trace_id}


def _run_summary_from_row(run_row: Any) -> TraceRunSummary:
    """从 runs 表行构造 TraceRunSummary（多个端点共用）。"""
    return TraceRunSummary(
        trace_id=run_row["trace_id"], workspace_id=run_row["workspace_id"],
        thread_id=run_row["thread_id"] or "", session_name=run_row["session_name"] or "",
        workspace_path="", endpoint=run_row["endpoint"] or "",
        status=run_row["status"],  # type: ignore[arg-type]
        started_at=run_row["started_at"] or "", ended_at=run_row["ended_at"],
        duration_ms=run_row["duration_ms"], event_count=run_row["event_count"] or 0,
        path="", error=run_row["error"],
    )


def _load_events(trace_id: str) -> list[TraceLogEvent]:
    """从 event_payloads 表还原事件列表。"""
    rows = db.query_all(
        "SELECT payload_json FROM event_payloads WHERE trace_id = ? ORDER BY sequence",
        (trace_id,),
    )
    return [TraceLogEvent.model_validate(json.loads(r["payload_json"])) for r in rows]


def _reconstruct_incremental_inputs(events: list[TraceLogEvent]) -> list[TraceLogEvent]:
    """对增量存储的 LLM input 做重建（Phase 3 T3.3）。

    后端 recorder 把 LLM input 写成增量（Phase 1），evolution 摄入保持增量存储
    （D4/D9 控空间）。详情视图/投影需要完整 input 时，顺着 anchor 链回溯重建。

    range 为空的事件（全量，T8）不受影响。无 llm_start 事件或链断裂时原样返回。

    性能：单次 O(N) 正向扫描批量重建（替代旧 O(M×N) 逐条重建）。
    """
    from app.ingestion.increment import reconstruct_all_inputs

    has_llm_start = any(e.type == "llm_start" for e in events)
    if not has_llm_start:
        return events

    events_raw = [e.model_dump() for e in events]
    # 单次 O(N) 批量重建，返回 {event_id: 完整 input}
    reconstructed = reconstruct_all_inputs(events_raw)
    if not reconstructed:
        return events

    for event_raw in events_raw:
        full_input = reconstructed.get(event_raw["event_id"])
        if full_input is not None:
            event_raw["input"] = full_input
    return [TraceLogEvent.model_validate(e) for e in events_raw]
