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
from app.core.models import TraceDetail, TraceLogEvent, TraceNode, TraceRunSummary

router = APIRouter(tags=["traces"])


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


@router.get("/traces", response_model=list[TraceListItem])
def list_traces(
    workspace: str | None = Query(None, description="按 workspace_id 过滤"),
    thread_id: str | None = Query(None, description="按 thread_id 过滤"),
    status: str | None = Query(None, description="按 status 过滤"),
    owner: str | None = Query(None, description="按 owner_user_id 过滤（D16 防串户）"),
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
        )
        for r in rows
    ]


@router.get("/traces/{trace_id}", response_model=TraceDetail)
def get_trace(trace_id: str) -> TraceDetail:
    """trace 详情：run + nodes + context + todos（重新投影还原）。"""
    run_row = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run_row is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    run = TraceRunSummary(
        trace_id=run_row["trace_id"], workspace_id=run_row["workspace_id"],
        thread_id=run_row["thread_id"] or "", session_name=run_row["session_name"] or "",
        workspace_path="", endpoint=run_row["endpoint"] or "",
        status=run_row["status"],  # type: ignore[arg-type]
        started_at=run_row["started_at"] or "", ended_at=run_row["ended_at"],
        duration_ms=run_row["duration_ms"], event_count=run_row["event_count"] or 0,
        path="", error=run_row["error"],
    )

    # 重新投影 event_payloads → context/todos（nodes 直接查表，但投影也产出 nodes，
    # 为保证 nodes/context/todos 一致，统一重新投影）
    events = _load_events(trace_id)
    # 增量重建（Phase 3 T3.3）：把增量存储的 LLM input 还原成完整 input，
    # 让投影/judge 看到完整内容。range 为空的事件（全量）不受影响。
    events = _reconstruct_incremental_inputs(events)
    projection = projector.TraceProjector().project(run, events)
    return TraceDetail(
        run=run, events=events, nodes=projection.nodes,
        context=projection.context, todos=projection.todos,
    )


@router.delete("/traces/{trace_id}")
def delete_trace(trace_id: str) -> dict[str, str]:
    """删除 trace 的 evolution 记录（runs/nodes/events/flags 随级联删除）。"""
    cur = db.execute("DELETE FROM runs WHERE trace_id = ?", (trace_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"status": "ok", "deleted": trace_id}


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
    """
    from app.improvement.increment import reconstruct_full_input

    has_llm_start = any(e.type == "llm_start" for e in events)
    if not has_llm_start:
        return events

    # 重建需要 dict 形态（increment.reconstruct_full_input 操作 dict）。
    events_raw = [e.model_dump() for e in events]
    # ⚠️ 必须「先算后写」：reconstruct_full_input 遍历 events_raw 时会读取每条
    # llm_start 的 input（增量尾部）。若边算边把重建出的「完整 input」写回
    # events_raw，后续事件的重建就会读到前序已被膨胀成完整 input 的事件，并把它
    # 当作增量尾部再次累加 → collected_messages 指数级膨胀，几个增量事件就能
    # 把内存撑爆（MemoryError → Internal Server Error）。
    # 因此先把所有重建结果算到独立映射，循环结束后再统一写回，保证遍历期间
    # events_raw 始终是原始增量 input。
    reconstructed: dict[str, Any] = {}
    for event_raw in events_raw:
        if event_raw.get("type") != "llm_start":
            continue
        if event_raw.get("input_context_range") is None:
            continue  # 全量，无需重建
        reconstructed[event_raw["event_id"]] = reconstruct_full_input(events_raw, event_raw)
    for event_raw in events_raw:
        full_input = reconstructed.get(event_raw["event_id"])
        if full_input is not None:
            event_raw["input"] = full_input
    return [TraceLogEvent.model_validate(e) for e in events_raw]
