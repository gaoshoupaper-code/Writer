"""trace 查看路由：取代后端 GET /threads/{tid}/traces 系列。

维度：全局 / trace_id（不依赖 thread 鉴权，纯内部工具）。
详情通过重新投影 event_payloads 还原完整 TraceDetail（含 context/todos）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import app.core.db as db
from app.ingestion import projector
from app.core.models import (
    TraceContextSegment,
    TraceDetail,
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
    """trace 列表项（runs 表行 + 命中规则数 + 用户名映射）。"""
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
    owner_user_id: str = "unknown"   # 归属用户 ID（Phase 3 D16）
    owner_username: str | None = None   # 用户名（LEFT JOIN user_cache，映射不到时 None）
    run_purpose: str = "user_generation"   # trace 来源（D2：区分执行端/进化端）


class TraceListResponse(BaseModel):
    """trace 列表分页响应（含 total 供前端分页器计算页码）。"""
    items: list[TraceListItem]
    total: int        # 满足当前过滤条件的总条数
    limit: int
    offset: int


@router.get("/traces", response_model=TraceListResponse)
def list_traces(
    workspace: str | None = Query(None, description="按 workspace_id 过滤"),
    thread_id: str | None = Query(None, description="按 thread_id 过滤"),
    status: str | None = Query(None, description="按 status 过滤"),
    owner: str | None = Query(None, description="按 owner_user_id 过滤（D16 防串户）"),
    run_purpose: str | None = Query(None, description="按 run_purpose 过滤（evolution_eval/evolution_evolve/user_generation）"),
    since: str | None = Query(None, description="ISO 8601 时间戳，只返回 started_at >= since 的 trace"),
    until: str | None = Query(None, description="ISO 8601 时间戳，只返回 started_at <= until 的 trace"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> TraceListResponse:
    """全局 trace 列表，按 started_at 倒序。

    LEFT JOIN user_cache 把 owner_user_id 映射成可读 username（进化端 trace
    owner='unknown' JOIN 不到时 owner_username=None）。时间范围过滤用 started_at
    字符串比较（ISO 格式天然有序），NULL started_at 不匹配会被排除。
    """
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
    if since:
        where.append("r.started_at >= ?")
        params.append(since)
    if until:
        where.append("r.started_at <= ?")
        params.append(until)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # total 条数（同一 where，不含 LIMIT/OFFSET）
    total_row = db.query_one(
        f"SELECT COUNT(*) AS c FROM runs r {where_sql}",
        tuple(params),
    )
    total = total_row["c"] if total_row else 0

    rows = db.query_all(
        f"""SELECT r.*, uc.username AS owner_username
            FROM runs r
            LEFT JOIN user_cache uc ON r.owner_user_id = uc.user_id
            {where_sql}
            ORDER BY r.started_at DESC LIMIT ? OFFSET ?""",
        tuple(params + [limit, offset]),
    )
    return TraceListResponse(
        items=[
            TraceListItem(
                trace_id=r["trace_id"], workspace_id=r["workspace_id"],
                thread_id=r["thread_id"], session_name=r["session_name"],
                endpoint=r["endpoint"], status=r["status"],
                started_at=r["started_at"], ended_at=r["ended_at"],
                duration_ms=r["duration_ms"], event_count=r["event_count"] or 0,
                error=r["error"], flag_count=0,
                owner_user_id=r.get("owner_user_id") or "unknown",
                owner_username=r.get("owner_username"),
                run_purpose=r.get("run_purpose") or "user_generation",
            )
            for r in rows
        ],
        total=total, limit=limit, offset=offset,
    )


def load_trace_detail(trace_id: str) -> TraceDetail | None:
    """加载完整 trace（含 events + context + nodes + todos），供内部消费。

    与 get_trace 路由的区别：本函数返回完整 TraceDetail（events/context 齐全），
    供评估/进化端内部调用（如 compute_flow_metrics 需遍历 events 算流程硬指标，
    read_trace_node/range 需查 context）；get_trace 路由调本函数后收窄成
    TraceDetailLite 返前端（events/context 太大走懒加载）。

    trace 不存在返回 None。
    """
    run_row = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run_row is None:
        return None
    run = _run_summary_from_row(run_row)
    events = _reconstruct_incremental_inputs(_load_events(trace_id))
    projection = projector.TraceProjector().project(run, events)
    return TraceDetail(
        run=run, events=events,
        nodes=projection.nodes, context=projection.context,
        todos=projection.todos,
    )


@router.get("/traces/{trace_id}", response_model=TraceDetailLite)
def get_trace(trace_id: str) -> TraceDetailLite:
    """trace 详情（轻量）：run + nodes + todos。

    events 和 context 不再全量返回（它们是 trace 最大的两块数据）。
    前端打开抽屉时通过 /events 和 /context 懒加载接口按需拉取。

    内部加载委托给 load_trace_detail（复用完整加载逻辑），此处收窄成 Lite。
    """
    detail = load_trace_detail(trace_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return TraceDetailLite(
        run=detail.run, nodes=detail.nodes, todos=detail.todos,
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


# ── trace 稳定性重构（设计 20260720_203000）：Pull 模式三接口 ──────────────
# 这三个接口是 Pull 主导架构的前端主力：游标拉事件 / 反查活跃 session / 收敛 interrupted。


class TraceEventsSinceResponse(BaseModel):
    """游标增量拉事件响应（trace 详情页 1s 轮询主力）。"""
    events: list[TraceLogEvent]
    max_seq: int            # 本次返回的最大 sequence（前端下次 since_seq）；无事件时 = since_seq
    has_more: bool          # 是否还有更多事件未拉（前端可立即续拉）
    trace_status: str       # 顺手返回 trace 状态，省前端一次 /traces/{id} 请求


@router.get("/traces/{trace_id}/events/since", response_model=TraceEventsSinceResponse)
def get_trace_events_since(
    trace_id: str,
    since_seq: int = Query(0, ge=0, description="返回 sequence > since_seq 的事件"),
    limit: int = Query(500, ge=1, le=1000, description="单次返回上限"),
) -> TraceEventsSinceResponse:
    """按 sequence 游标增量拉事件（trace 稳定性重构，Pull 主导）。

    路径与现有 /events（按 event_id 批量拉，抽屉懒加载）错开——前者是详情页轮询
    主力（高频，带游标），后者是抽屉懒加载（低频，按需）。两者共存不冲突。

    性能策略：**不做增量 input 重建**（_reconstruct_incremental_inputs O(N)）——
    轮询只需事件数量和节点信息，input 重建留给用户点开抽屉时的 /events 接口。
    这样 1s 轮询保持轻量（单次 SELECT 命中 idx_events_trace 索引）。
    """
    run_row = db.query_one("SELECT status FROM runs WHERE trace_id = ?", (trace_id,))
    if run_row is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    rows = db.query_all(
        """SELECT payload_json FROM event_payloads
           WHERE trace_id=? AND sequence>?
           ORDER BY sequence LIMIT ?""",
        (trace_id, since_seq, limit + 1),  # limit+1 探测 has_more
    )
    has_more = len(rows) > limit
    rows = rows[:limit]

    events: list[TraceLogEvent] = []
    max_seq = since_seq
    for r in rows:
        evt = TraceLogEvent.model_validate(json.loads(r["payload_json"]))
        events.append(evt)
        if evt.sequence > max_seq:
            max_seq = evt.sequence

    return TraceEventsSinceResponse(
        events=events,
        max_seq=max_seq,
        has_more=has_more,
        trace_status=run_row["status"],
    )


class ActiveSessionResponse(BaseModel):
    """trace 当前被哪个 session 跑（trace 详情页停止按钮反查用）。"""
    session_type: str | None   # evolve / eval / test / null（无活跃 session）
    session_id: str | None     # session 主键值
    stop_endpoint: str | None  # 顺手算好的 stop 端点，前端直接调；null = 无停止入口


# session_type → (表名, 主键列, stop 端点模板)。与 recorder._SESSION_TABLE_MAP 对齐
# （test 不在此列——测试用 executor 跑 trace，evolution 端不自观测录像，无 stop 反查）。
_ACTIVE_SESSION_TABLES: dict[str, tuple[str, str, str]] = {
    "evolve": ("evolve_sessions", "session_id", "/api/evolve/sessions/{}/stop"),
    "eval": ("evaluation_sessions", "eval_id", "/api/eval-agent/sessions/{}/stop"),
    "test": ("manual_tests", "test_id", "/api/tests/{}/stop"),
}


@router.get("/traces/{trace_id}/active-session", response_model=ActiveSessionResponse)
def get_active_session(trace_id: str) -> ActiveSessionResponse:
    """反查该 trace 被哪个活跃 session 跑（trace 详情页停止按钮前置查询）。

    语义：trace_id 可能是 session 的 self_trace_id（进化/评估自观测）或 manual_tests.trace_id
    （被测对象）。前者用 self_trace_id 查 evolve/eval session，后者用 trace_id 查 manual_tests。
    只返回"活跃"session（status 不是终态）——已结束的 session 不提供停止入口。
    """
    # 查 evolve/eval session（按 self_trace_id 反查，且 session 状态非终态）
    for session_type, (table, key_col, stop_tpl) in _ACTIVE_SESSION_TABLES.items():
        trace_col = "trace_id" if session_type == "test" else "self_trace_id"
        # 各表的"活跃"状态判定：
        #   evolve_sessions.status: running/conversing/finalizing/pending_review（非 published/discarded/failed/cancelled）
        #   evaluation_sessions.status: running（非 done/failed）
        #   manual_tests.status: pending/running（非 done/failed）
        if session_type == "evolve":
            active_clause = "status NOT IN ('published', 'discarded', 'failed', 'cancelled')"
        elif session_type == "eval":
            active_clause = "status = 'running'"
        else:
            active_clause = "status IN ('pending', 'running')"
        row = db.query_one(
            f"SELECT {key_col} AS sid FROM {table} WHERE {trace_col}=? AND {active_clause} LIMIT 1",
            (trace_id,),
        )
        if row is not None:
            return ActiveSessionResponse(
                session_type=session_type,
                session_id=row["sid"],
                stop_endpoint=stop_tpl.format(row["sid"]),
            )
    # 无活跃 session（trace 已结束 / 外部摄入 trace / session 已终态）
    return ActiveSessionResponse(session_type=None, session_id=None, stop_endpoint=None)


class ResolveRequest(BaseModel):
    """用户收敛 interrupted trace 的请求体。"""
    target_status: str       # "failed" | "completed"
    note: str | None = None  # 可选用户备注（记入 error 字段前缀）


@router.post("/traces/{trace_id}/resolve")
def resolve_trace(trace_id: str, req: ResolveRequest) -> dict[str, str]:
    """用户手动收敛 interrupted trace（设计 20260720_203000）。

    interrupted 不是真失败，是"未知状态"——用户在 UI 上判断后手动标为 failed 或 completed。
    仅 status='interrupted' 时允许；其它状态返回 409 Conflict。

    收敛时触发 nodes 投影（interrupted 时 _finalize_run 没跑，nodes 可能未生成），
    并写一条 run_meta 事件记录用户决策（便于审计）。
    """
    if req.target_status not in ("failed", "completed"):
        raise HTTPException(status_code=422, detail="target_status 必须是 failed 或 completed")

    run_row = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run_row is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    if run_row["status"] != "interrupted":
        raise HTTPException(
            status_code=409,
            detail=f"仅 interrupted 状态可收敛，当前状态: {run_row['status']}",
        )

    # error 字段：拼接用户备注（若有），便于审计追溯。
    error_msg = req.note or run_row.get("error") or ""
    if req.target_status == "failed" and req.note:
        error_msg = f"[用户标记失败] {req.note}"
    elif req.target_status == "completed":
        error_msg = req.note or ""

    # UPDATE runs 到目标终态。
    db.execute(
        """UPDATE runs
           SET status=?, interrupted_reason='user_marked', ended_at=COALESCE(ended_at, ?), error=?
           WHERE trace_id=?""",
        (req.target_status, datetime.now(UTC).isoformat(), error_msg, trace_id),
    )

    # 补投影 nodes（interrupted 时 _finalize_run 没跑，前端列表/详情可能缺 nodes）。
    # 用 try 兜底——投影失败不能阻断收敛（用户至少要把状态改对）。
    try:
        _project_nodes_for_trace(trace_id, req.target_status)
    except Exception:
        # 投影失败记日志但不抛——状态收敛已成功，nodes 可后续重试。
        pass

    return {"status": "ok", "resolved_to": req.target_status, "trace_id": trace_id}


def _project_nodes_for_trace(trace_id: str, status: str) -> None:
    """为 trace 补投影 nodes（resolve_trace 调用，interrupted 收敛时用）。

    复用 load_trace_detail 的投影逻辑，但只写 nodes（不重新算 run）。
    幂等：先删旧再插。
    """
    detail = load_trace_detail(trace_id)
    if detail is None or not detail.nodes:
        return
    db.execute("DELETE FROM nodes WHERE trace_id=?", (trace_id,))
    db.executemany(
        """INSERT INTO nodes
           (node_id, trace_id, parent_node_id, kind, label, status,
            agent_name, agent_role, depth, started_at, ended_at,
            duration_ms, model_name, tool_name, skill_name,
            usage_input, usage_output, usage_total, chain_summary, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                n.node_id, trace_id, n.parent_node_id, n.kind, n.label,
                n.status, n.agent_name, n.agent_role, n.depth,
                n.started_at, n.ended_at, n.duration_ms, n.model_name,
                n.tool_name, n.skill_name,
                n.usage.input_tokens if n.usage else None,
                n.usage.output_tokens if n.usage else None,
                n.usage.total_tokens if n.usage else None,
                n.chain_summary, n.error,
            )
            for n in detail.nodes
        ],
    )


def _run_summary_from_row(run_row: Any) -> TraceRunSummary:
    """从 runs 表行构造 TraceRunSummary（多个端点共用）。

    trace 稳定性重构新增字段 last_heartbeat_at / interrupted_reason 用 _row_get 兜底，
    老库或查询未含此列时返回 None（sqlite3.Row 和 dict 都支持 .get）。
    """
    return TraceRunSummary(
        trace_id=run_row["trace_id"], workspace_id=run_row["workspace_id"],
        thread_id=run_row["thread_id"] or "", session_name=run_row["session_name"] or "",
        workspace_path="", endpoint=run_row["endpoint"] or "",
        status=run_row["status"],  # type: ignore[arg-type]
        started_at=run_row["started_at"] or "", ended_at=run_row["ended_at"],
        duration_ms=run_row["duration_ms"], event_count=run_row["event_count"] or 0,
        path="", error=run_row["error"],
        last_heartbeat_at=_row_get(run_row, "last_heartbeat_at"),
        interrupted_reason=_row_get(run_row, "interrupted_reason"),
    )


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """从 sqlite3.Row / dict 安全取值，列不存在时返回 default。

    sqlite3.Row 的 keys() 返回列名列表；dict 直接 in 判断。两者都支持 .get，
    但 sqlite3.Row 的 .get 在列不存在时会抛 IndexError 而非返回 default，
    所以这里显式做 keys 检查。
    """
    try:
        keys = row.keys() if hasattr(row, "keys") else []
        if key in keys:
            return row[key]
        return row.get(key, default) if hasattr(row, "get") else default
    except (KeyError, IndexError):
        return default


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
