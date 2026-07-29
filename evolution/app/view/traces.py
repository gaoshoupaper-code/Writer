"""trace 查看路由：取代后端 GET /threads/{tid}/traces 系列。

维度：全局 / trace_id（不依赖 thread 鉴权，纯内部工具）。
详情通过重新投影 event_payloads 还原完整 TraceDetail（含 context/todos）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

import app.core.db as db
from app.ingestion import projector
from app.trace_payloads import delete_trace_payloads, hydrate_event, read_payload
from app.trace.access import audit_content_access, require_full_content_access
from app.core.models import (
    CancelAudit,
    TraceContextSegment,
    TraceDetail,
    TraceLogEvent,
    TraceNode,
    TraceRunSummary,
    TraceTodoSnapshot,
)

router = APIRouter(tags=["traces"])


def _require_full_content_access(request: Request) -> None:
    require_full_content_access(request)


def _audit_content_access(
    request: Request, action: str, object_type: str, object_id: str
) -> None:
    audit_content_access(request, action, object_type, object_id)


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
    schema_version: int = 1
    service: str | None = None
    workload: str | None = None
    integrity_status: str = "legacy"
    coverage: dict[str, str] = Field(default_factory=dict)
    skill_activation_count: int = 0
    middleware_intervention_count: int = 0
    hitl_count: int = 0


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
    workload: str | None = Query(None, description="按 V2 workload 过滤"),
    integrity_status: str | None = Query(None, description="按 Trace 完整性过滤"),
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
    if workload:
        where.append("r.workload = ?")
        params.append(workload)
    if integrity_status:
        where.append("r.integrity_status = ?")
        params.append(integrity_status)
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
        f"""SELECT r.*, uc.username AS owner_username,
                   (SELECT COUNT(*) FROM event_payloads e
                    WHERE e.trace_id=r.trace_id AND e.type='skill_activation')
                       AS skill_activation_count,
                   (SELECT COUNT(*) FROM event_payloads e
                    WHERE e.trace_id=r.trace_id AND e.type='middleware_intervention')
                       AS middleware_intervention_count,
                   (SELECT COUNT(*) FROM event_payloads e
                    WHERE e.trace_id=r.trace_id AND e.type='hitl') AS hitl_count
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
                schema_version=int(r.get("schema_version") or 1),
                service=r.get("service"),
                workload=r.get("workload"),
                integrity_status=r.get("integrity_status") or "legacy",
                coverage=json.loads(r.get("coverage_json") or "{}"),
                skill_activation_count=int(r.get("skill_activation_count") or 0),
                middleware_intervention_count=int(r.get("middleware_intervention_count") or 0),
                hitl_count=int(r.get("hitl_count") or 0),
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
    events = _reconstruct_incremental_inputs([hydrate_event(event) for event in _load_events(trace_id)])
    projection = projector.TraceProjector().project(run, events)
    return TraceDetail(
        run=run, events=events,
        nodes=projection.nodes, context=projection.context,
        todos=projection.todos,
    )


def load_trace_structure(trace_id: str) -> TraceDetail | None:
    """Project reference-only events without reading governed payload bodies."""
    run_row = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run_row is None:
        return None
    run = _run_summary_from_row(run_row)
    events = [_structural_event(event) for event in _load_events(trace_id)]
    projection = projector.TraceProjector().project(run, events)
    return TraceDetail(
        run=run,
        events=events,
        nodes=projection.nodes,
        context=[],
        todos=[],
    )


@router.get("/traces/{trace_id}", response_model=TraceDetailLite)
def get_trace(trace_id: str) -> TraceDetailLite:
    """trace 详情（轻量）：run + nodes + todos。

    events 和 context 不再全量返回（它们是 trace 最大的两块数据）。
    前端打开抽屉时通过 /events 和 /context 懒加载接口按需拉取。

    内部加载委托给 load_trace_detail（复用完整加载逻辑），此处收窄成 Lite。
    """
    detail = load_trace_structure(trace_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return _to_trace_detail_lite(detail)


@router.post("/traces/{trace_id}/refresh", response_model=TraceDetailLite)
def refresh_executor_trace(trace_id: str, request: Request) -> TraceDetailLite:
    """按需增量同步 executor trace，并返回最新轻量详情。

    executor 的运行中 trace 默认不持续摄入；只有用户打开详情页时才调用本端点，
    因此既能实时查看单次测试，又不会让所有后台 trace 产生高频同步开销。
    """
    from app.ingestion.ingestion import ingest_trace_now

    traceparent = request.headers.get("traceparent")
    if ingest_trace_now(trace_id, traceparent) is None:
        raise HTTPException(status_code=404, detail="Trace 尚未可从 executor 读取")
    detail = load_trace_structure(trace_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Trace 同步后仍不可用")
    return _to_trace_detail_lite(detail)


def _to_trace_detail_lite(detail: TraceDetail) -> TraceDetailLite:
    return TraceDetailLite(
        run=detail.run, nodes=detail.nodes, todos=detail.todos,
    )


class IntegrityCheck(BaseModel):
    """单个完整性检查项：具体失败检查 + 影响阶段 + 恢复动作（FR-003 / AC-004）。

    取代"Trace 数据不完整：不完整"这类同义反复——每项必须说清缺了什么、影响哪个
    下游、用户能做什么，不得只给一个标签或比例。
    """

    check: str             # 失败检查名（如 "payload_missing"/"manifest_capture_degraded"）
    stage: str             # 影响阶段（如 "采集"/"摄入"/"完整性校验"）
    impact: str            # 对下游的影响（如 "证据卷宗无法编纂为 ready"）
    recovery: str          # 可执行恢复动作（如 "重跑该测试生成新 Trace"）


class IntegrityDiagnosis(BaseModel):
    """Trace 完整性结构化诊断（FR-003 / AC-004 / DEC-006）。

    verified 的 Trace 也会返回（空 missing_checks），让前端用同一套渲染逻辑。
    """

    trace_id: str
    integrity_status: str           # verified / incomplete / conflict / legacy
    recoverable: bool               # 正文是否可恢复（历史丢失=false，重跑可恢复=true）
    missing_checks: list[IntegrityCheck]
    affected_downstreams: list[str]  # 受影响的下游（evaluation/evolution/evidence_compile）


def _compute_integrity_diagnosis(trace_id: str) -> IntegrityDiagnosis:
    """根据 runs + trace_receipts 推导结构化完整性诊断。

    数据源：
      - runs.integrity_status：权威完整性标签
      - trace_receipts：manifest_status / manifest_json（含 capture_degraded）/
        missing_ranges_json（序列缺口）
    诊断规则（FR-003）：每个非 verified 状态必须给出具体失败检查、影响和恢复动作。
    """
    run_row = db.query_one("SELECT * FROM runs WHERE trace_id = ?", (trace_id,))
    if run_row is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    integrity = run_row.get("integrity_status") or "legacy"
    schema_version = int(run_row.get("schema_version") or 1)

    # legacy / schema_version<2：旧版 Trace，无法套用 V2 完整性结论（DEC-006）。
    if integrity == "legacy" or schema_version < 2:
        return IntegrityDiagnosis(
            trace_id=trace_id,
            integrity_status="legacy",
            recoverable=True,
            missing_checks=[
                IntegrityCheck(
                    check="legacy_trace_unverified",
                    stage="完整性校验",
                    impact="该 Trace 由旧版系统生成，未经过 V2 完整性校验，不能作为下游证据基线",
                    recovery="重新运行该任务以生成 V2 Trace，再编纂证据卷宗",
                )
            ],
            affected_downstreams=["evidence_compile", "evaluation", "evolution"],
        )

    # FR-008/AC-010：pending 是记录/封存中的中性态——既非完整也非损坏。
    # UI 不得把它当终态 incomplete 故障展示（EVD-005 根因）。下游门禁照常关闭（pending
    # 不可消费），但诊断告知用户"记录中/校验中"，等终态再判，而非"数据有缺口"。
    if integrity == "pending":
        phase = run_row.get("trace_phase") or "recording"
        phase_label = {"recording": "记录中", "sealing": "封存中", "degraded": "封存异常"}.get(phase, "处理中")
        return IntegrityDiagnosis(
            trace_id=trace_id,
            integrity_status="pending",
            recoverable=True,
            missing_checks=[
                IntegrityCheck(
                    check="integrity_pending",
                    stage="完整性校验",
                    impact=f"Trace 正在{phase_label}，尚未进行完整性校验——这不是数据损坏",
                    recovery="等待运行结束并封存后，完整性会自动校验；无需重新运行",
                )
            ],
            affected_downstreams=[],  # pending 不算缺口，下游只是暂时等待
        )

    if integrity == "verified":
        return IntegrityDiagnosis(
            trace_id=trace_id,
            integrity_status="verified",
            recoverable=True,
            missing_checks=[],
            affected_downstreams=[],
        )

    # incomplete / conflict：从 receipt 提取具体缺口（AC-004）。
    receipt = db.query_one(
        "SELECT manifest_json, manifest_status, missing_ranges_json FROM trace_receipts WHERE trace_id=?",
        (trace_id,),
    )
    checks: list[IntegrityCheck] = []
    capture_degraded = False
    missing_ranges: list[Any] = []

    if receipt is not None:
        missing_ranges = json.loads(receipt.get("missing_ranges_json") or "[]")
        manifest_raw = receipt.get("manifest_json")
        if manifest_raw:
            try:
                manifest = json.loads(manifest_raw)
                capture_degraded = bool(manifest.get("capture_degraded"))
            except (json.JSONDecodeError, TypeError):
                checks.append(
                    IntegrityCheck(
                        check="manifest_unparseable",
                        stage="完整性校验",
                        impact="终态清单无法解析，无法证明事件序列未被篡改",
                        recovery="重新运行该任务以生成有效清单",
                    )
                )
        if receipt.get("manifest_status") in (None, "", "missing"):
            checks.append(
                IntegrityCheck(
                    check="manifest_missing",
                    stage="终态收尾",
                    impact="缺少终态清单，无法证明 Trace 数据完整",
                    recovery="重新运行该任务以生成完整终态",
                )
            )

    if missing_ranges:
        checks.append(
            IntegrityCheck(
                check="event_sequence_gap",
                stage="摄入",
                impact=f"事件序列存在 {len(missing_ranges)} 处缺口，调用链可能断裂",
                recovery="重新运行该任务，或等待摄入补全后刷新",
            )
        )

    if capture_degraded:
        # 采集降级：payload 被安全闸门拒绝（密钥/二进制）或写盘失败。
        # 注意：reasoning 剥离不再触发 degraded（DEC-001），此处仅指真实数据丢失。
        checks.append(
            IntegrityCheck(
                check="payload_capture_degraded",
                stage="采集",
                impact="部分 LLM 正文或工具载荷因安全策略或写盘失败被丢弃，业务证据不完整",
                recovery="重新运行该任务；若反复出现请检查载荷是否含密钥类内容",
            )
        )

    if not checks:
        # integrity 非 verified 但无具体缺口证据：给出兜底诊断，不返回空同义反复。
        checks.append(
            IntegrityCheck(
                check="integrity_unverified",
                stage="完整性校验",
                impact=f"完整性状态为 {integrity}，但缺少具体缺口证据",
                recovery="重新运行该任务以生成可验证的完整 Trace",
            )
        )

    # 历史正文永久丢失不可补造（DEC-006）：capture_degraded 导致的丢失不可原地恢复，
    # 只能重跑生成新 Trace。recoverable=True 表示"可通过重跑恢复"，而非"可原地修复"。
    return IntegrityDiagnosis(
        trace_id=trace_id,
        integrity_status=integrity,
        recoverable=True,
        missing_checks=checks,
        affected_downstreams=["evidence_compile", "evaluation", "evolution"],
    )


@router.get("/traces/{trace_id}/integrity", response_model=IntegrityDiagnosis)
def get_trace_integrity(trace_id: str) -> IntegrityDiagnosis:
    """Trace 完整性结构化诊断（FR-003 / AC-004）。

    返回具体失败检查、影响阶段、受影响下游和可执行恢复动作，取代前端
    "Trace 数据不完整：不完整"这类同义反复文案。verified Trace 返回空 missing_checks。
    """
    return _compute_integrity_diagnosis(trace_id)


@router.get("/traces/{trace_id}/events", response_model=list[TraceLogEvent])
def get_trace_events(
    request: Request,
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
    _require_full_content_access(request)

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

    events = [hydrate_event(TraceLogEvent.model_validate(json.loads(r["payload_json"]))) for r in rows]

    # 增量重建（只针对本次拉取的事件集——但如果需要完整重建，
    # 前端应拉全链事件。这里对拉取的 llm_start 做单条重建降级处理）
    has_incremental = any(
        e.type == "llm_start" and e.input_context_range is not None for e in events
    )
    if has_incremental:
        # 需要全链重建：加载该 trace 所有事件做批量重建，再筛出请求的
        all_events = _reconstruct_incremental_inputs([hydrate_event(event) for event in _load_events(trace_id)])
        wanted = {eid for eid in id_list}
        events = [e for e in all_events if e.event_id in wanted]
    _audit_content_access(request, "view", "trace_events", trace_id)
    return events


@router.get("/traces/{trace_id}/context", response_model=TraceContextSegment)
def get_trace_context(
    request: Request,
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
    _require_full_content_access(request)

    # context 在投影时产出，没有单独的表——需要投影后按 anchor_id 筛选。
    # 对大 trace 这仍有开销，但只在用户主动打开抽屉时触发（非首屏）。
    events = _reconstruct_incremental_inputs([hydrate_event(event) for event in _load_events(trace_id)])
    run = _run_summary_from_row(run_row)
    projection = projector.TraceProjector().project(run, events)
    for seg in projection.context:
        if seg.anchor_id == anchor_id:
            _audit_content_access(request, "view", "trace_context", trace_id)
            return seg
    raise HTTPException(status_code=404, detail="Context segment not found")


@router.delete("/traces/{trace_id}")
def delete_trace(trace_id: str) -> dict[str, str]:
    """删除 trace 的 evolution 记录（runs/nodes/events/flags/证据包 随级联删除）。"""
    # 证据卷宗是逻辑外键（无物理 FK），需显式级联删除——证据卷宗冻结了用户需求和正文，
    # 删除原 trace 时必须一并清理，避免冻结内容脱离原 trace 留存（需求 D-删除语义）。
    # 阶段 F 会强化：同步删评估卷宗、纠错裁决、未发布进化记录。
    delete_trace_payloads(trace_id)
    db.execute("DELETE FROM evidence_dossiers WHERE trace_id = ?", (trace_id,))
    cur = db.execute("DELETE FROM runs WHERE trace_id = ?", (trace_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"status": "ok", "deleted": trace_id}


class PayloadSearchMatch(BaseModel):
    payload_id: str
    trace_ids: list[str]
    excerpt: str


@router.get("/trace-content/payloads/{payload_id}")
def get_trace_payload_body(payload_id: str, request: Request) -> Any:
    _require_full_content_access(request)
    payload = read_payload(payload_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Payload not found or expired")
    _audit_content_access(request, "view", "payload", payload_id)
    return payload


@router.get("/trace-content/search", response_model=list[PayloadSearchMatch])
def search_trace_payloads(
    request: Request,
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(50, ge=1, le=100),
) -> list[PayloadSearchMatch]:
    """Explicit super-admin search; payload text is never copied into a hot index."""
    _require_full_content_access(request)
    rows = db.query_all(
        """SELECT payload_id FROM payload_objects
           WHERE deleted_at IS NULL AND (expires_at IS NULL OR expires_at > ?)
           ORDER BY created_at DESC""",
        (datetime.now(UTC).isoformat(),),
    )
    matches: list[PayloadSearchMatch] = []
    needle = q.casefold()
    for row in rows:
        value = read_payload(row["payload_id"])
        if value is None:
            continue
        rendered = json.dumps(value, ensure_ascii=False)
        index = rendered.casefold().find(needle)
        if index < 0:
            continue
        trace_rows = db.query_all(
            "SELECT DISTINCT trace_id FROM trace_payload_links WHERE payload_id=?",
            (row["payload_id"],),
        )
        start = max(0, index - 120)
        matches.append(
            PayloadSearchMatch(
                payload_id=row["payload_id"],
                trace_ids=[item["trace_id"] for item in trace_rows],
                excerpt=rendered[start : index + len(q) + 120],
            )
        )
        if len(matches) >= limit:
            break
    _audit_content_access(request, "search", "payload", "query")
    return matches


@router.get("/trace-content/traces/{trace_id}/export")
def export_trace_content(trace_id: str, request: Request) -> dict[str, Any]:
    _require_full_content_access(request)
    detail = load_trace_detail(trace_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    _audit_content_access(request, "export", "trace", trace_id)
    return detail.model_dump(mode="json")


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
        evt = _structural_event(TraceLogEvent.model_validate(json.loads(r["payload_json"])))
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
        schema_version=int(_row_get(run_row, "schema_version", 1) or 1),
        service=_row_get(run_row, "service"),
        workload=_row_get(run_row, "workload"),
        purpose=_row_get(run_row, "run_purpose"),
        integrity_status=_row_get(run_row, "integrity_status", "legacy") or "legacy",
        coverage=json.loads(_row_get(run_row, "coverage_json", "{}") or "{}"),
        run_snapshot=json.loads(_row_get(run_row, "run_snapshot_json", "{}") or "{}"),
        links=json.loads(_row_get(run_row, "links_json", "[]") or "[]"),
        external_refs=json.loads(_row_get(run_row, "external_refs_json", "{}") or "{}"),
        # 四维正交生命周期（DEC-008）：phase/cancel_audit/revision 透传给桌面统一投影。
        trace_phase=_row_get(run_row, "trace_phase"),
        cancel_audit=_parse_cancel_audit(_row_get(run_row, "cancel_audit")),
        lifecycle_revision=int(_row_get(run_row, "lifecycle_revision", 0) or 0),
    )


def _parse_cancel_audit(raw: Any) -> CancelAudit | None:
    """从 runs.cancel_audit（JSON 文本）解析 CancelAudit，None/损坏返回 None。"""
    if not raw:
        return None
    try:
        return CancelAudit.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


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


def _structural_event(event: TraceLogEvent) -> TraceLogEvent:
    """Remove both V2 references' bodies and legacy inline semantic content."""
    values = event.model_dump()
    for field_name in ("input", "output", "tool_args", "tool_output"):
        values[field_name] = None
    if isinstance(values.get("tool_calls"), list):
        values["tool_calls"] = [
            {key: value for key, value in call.items() if key != "args"}
            if isinstance(call, dict)
            else call
            for call in values["tool_calls"]
        ]
    return TraceLogEvent.model_validate(values)


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
