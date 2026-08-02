"""证据卷宗编译 API（Evidence Dossier）。

端点：
  POST /dossier/start                        启动编译（幂等：同规则版本已有 ready/partial 则直接返回）
  GET  /dossier/sessions/{dossier_id}        查编译状态 + 卷宗内容
  POST /dossier/sessions/{dossier_id}/stop   取消编译
  GET  /dossier/packs/{dossier_id}/drill/{evidence_id}  按证据 ID 回钻原始片段（权限校验）
  GET  /dossier/traces/{trace_id}/packs      列出 trace 的所有证据卷宗版本
  GET  /dossier/traces/{trace_id}/current    查当前推荐版本

编译是纯后台计算（无 Agent 对话流），用 asyncio.create_task + asyncio.to_thread 跑。

术语映射（2026-07-27）：原名 evidence（轨迹证据包），现统一为 dossier（证据卷宗）。
对外用 dossier_id；URL 路径段 /packs/ 保留（语义即卷宗包，前端已用，避免无谓破坏）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import app.core.db as db
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.dossier import repo
from app.dossier.extractor import COMPILE_RULE_VERSION
from app.dossier.eligibility import list_creation_trace_candidates
from app.dossier.evidence_override import (
    EvidenceOverrideError,
    approve_evidence_override,
    revoke_evidence_override,
)
from app.view.traces import load_trace_detail
from app.trace.access import require_product_owner
from app.trace.facts import (
    ConsumptionRejected,
    add_lineage,
    require_verified_creation_trace,
)
from app.trace.recorder import EvolutionTraceRecorder
from contracts.trace import TraceSpanLink

logger = logging.getLogger("evolution.dossier.api")

router = APIRouter(prefix="/dossier", tags=["dossier"])

# dossier_id → 后台编译 task。stop 端点靠它 cancel。
_running_tasks: dict[str, asyncio.Task] = {}


@router.get("/candidates")
def list_candidates(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """列出与启动接口使用同一资格规则的证据编纂候选。"""
    return list_creation_trace_candidates(limit=limit, offset=offset)


# ── 启动编译 ──────────────────────────────────────────────────


class CompileStartRequest(BaseModel):
    trace_id: str


class CompileStartResponse(BaseModel):
    dossier_id: str
    trace_id: str
    status: str  # started | ready | partial | compiling
    compile_trace_id: str | None = None


def get_recorder() -> EvolutionTraceRecorder | None:
    from app.main import app
    return getattr(app.state, "trace_recorder", None)


@router.post("/start", response_model=CompileStartResponse, status_code=202)
async def start_compile(req: CompileStartRequest) -> CompileStartResponse:
    """启动证据卷宗编译（异步）。立即返回 dossier_id，后台跑编译。

    幂等：
      - 同 trace + 同编译规则版本已有 ready/partial 卷宗 → 直接返回（不重复编译）
      - 同 trace + 同编译规则版本正在编译 → 直接返回该卷宗（不重复触发）
      - 否则新建 pending 卷宗 + 后台编译
    """
    trace_id = req.trace_id

    try:
        run_row = require_verified_creation_trace(trace_id)
    except ConsumptionRejected as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "integrity_status": exc.integrity_status,
                "missing_fields": list(exc.missing_fields),
            },
        ) from exc

    owner_user_id = run_row.get("owner_user_id") or "unknown"

    # 幂等 1：同规则版本已有可消费卷宗 → 直接返回
    existing = repo.get_consumable_by_rule(trace_id, COMPILE_RULE_VERSION)
    if existing:
        return CompileStartResponse(
            dossier_id=existing["dossier_id"], trace_id=trace_id, status=existing["status"],
            compile_trace_id=existing.get("compile_trace_id"),
        )

    # 幂等 2：同规则版本正在编译 → 直接返回
    active = repo.get_active_compiling(trace_id, COMPILE_RULE_VERSION)
    if active:
        return CompileStartResponse(
            dossier_id=active["dossier_id"], trace_id=trace_id, status=active["status"],
            compile_trace_id=active.get("compile_trace_id"),
        )

    # 新建卷宗 + 后台编译
    # provenance 判定（FR-004/DEC-004）：
    #   - 源 trace 带有效人工确认（已确认未撤回）→ partial（基于停止 trace 恢复的半成品）
    #   - 否则有 producer 同 trace 的产物 → trace_time（运行时产物）
    #   - 否则 → compile_time_snapshot（编纂时补采）
    provenance = _decide_dossier_provenance(trace_id)
    dossier_id = repo.create_dossier(
        trace_id, owner_user_id,
        compile_rule_version=COMPILE_RULE_VERSION,
        provenance=provenance,
    )
    recorder = get_recorder()
    if recorder is None:
        repo.update_dossier(
            dossier_id, status="failed", failure_reason="Trace recorder unavailable", finished=True
        )
        raise HTTPException(status_code=503, detail="Trace recorder unavailable")
    handle = recorder.create_run(
        session_id=dossier_id,
        run_purpose="evidence_compile",
        endpoint="dossier.compile",
        workload="evidence_compile",
        links=[
            TraceSpanLink(
                target_trace_id=trace_id,
                relation="consumes",
                attributes={"dossier_id": dossier_id},
            )
        ],
        external_refs={"dossier_id": dossier_id, "source_trace_id": trace_id},
    )
    compile_trace_id = handle.trace_id
    db.execute(
        "UPDATE evidence_dossiers SET compile_trace_id=? WHERE pack_id=?",
        (compile_trace_id, dossier_id),
    )
    add_lineage("trace", compile_trace_id, "consumes", "trace", trace_id)
    task = asyncio.create_task(
        _run_compile_bg(trace_id, dossier_id, compile_trace_id, recorder)
    )
    _running_tasks[dossier_id] = task

    logger.info("证据卷宗编译启动: dossier=%s trace=%s", dossier_id, trace_id)
    return CompileStartResponse(
        dossier_id=dossier_id,
        trace_id=trace_id,
        status="started",
        compile_trace_id=compile_trace_id,
    )


def _decide_dossier_provenance(trace_id: str) -> str:
    """判定新建卷宗的 provenance（FR-004/DEC-004）。

    partial 优先：源 trace 带有效人工确认（approved=1 且未撤回）→ partial。
    否则按既有规则：有运行时产物 → trace_time；无 → compile_time_snapshot。
    单查询合并：runs 行 + EXISTS 运行时产物一起取，减少 start_compile 热点 DB 往返。
    """
    row = db.query_one(
        """SELECT
             (evidence_override_approved=1 AND evidence_override_revoked_at IS NULL) AS is_partial,
             EXISTS(SELECT 1 FROM artifact_revisions WHERE producer_trace_id=r.trace_id LIMIT 1) AS has_runtime
           FROM runs r WHERE trace_id=?""",
        (trace_id,),
    )
    if row is None:
        return "compile_time_snapshot"
    if row.get("is_partial"):
        return "partial"
    return "trace_time" if row.get("has_runtime") else "compile_time_snapshot"


async def _run_compile_bg(
    trace_id: str,
    dossier_id: str,
    compile_trace_id: str,
    recorder: EvolutionTraceRecorder,
) -> None:
    """后台执行证据卷宗编译。

    compiler.compile_dossier 是同步函数（内含 httpx 阻塞 LLM 调用），
    用 asyncio.to_thread 丢线程池，保持事件循环响应。
    """
    from app.dossier.compiler import compile_dossier
    from app.trace.observers import TraceLlmObserver
    # DEC-001 / FR-002：把统一观测桥传入确定性编译器，让每个阶段和每次 llm.chat
    # 写进同一编纂 Trace，消除"只有 run_start/run_end、看不到 2 分多钟内部执行"。
    observer = TraceLlmObserver(recorder, compile_trace_id, component="dossier-compiler")
    try:
        result = await asyncio.to_thread(compile_dossier, trace_id, dossier_id, observer)
        if result.get("status") in {"ready", "partial"}:
            _record_compile_lineage(trace_id, dossier_id, compile_trace_id)
            recorder.complete_run(compile_trace_id)
        else:
            recorder.fail_run(compile_trace_id, result.get("reason") or "dossier compile failed")
    except asyncio.CancelledError:
        # 用户取消（CON-003：取消不是失败）。标 cancelled，recorder 收尾 cancelled。
        logger.info("证据卷宗编译 %s 被用户取消", dossier_id)
        repo.update_dossier(dossier_id, status="cancelled", failure_reason="用户取消", finished=True)
        recorder.cancel_run(compile_trace_id, reason="user_stop")
        raise
    except Exception as exc:
        logger.exception("证据卷宗编译 %s 后台异常", dossier_id)
        repo.update_dossier(dossier_id, status="failed", failure_reason=str(exc), finished=True)
        recorder.fail_run(compile_trace_id, exc)
    finally:
        _running_tasks.pop(dossier_id, None)


def _record_compile_lineage(
    source_trace_id: str, dossier_id: str, compile_trace_id: str
) -> None:
    add_lineage("trace", compile_trace_id, "produces", "evidence_dossier", dossier_id)
    revisions = db.query_all(
        """SELECT artifact_revision_id FROM artifact_revisions
           WHERE producer_trace_id=? OR source_trace_id=?""",
        (source_trace_id, source_trace_id),
    )
    for row in revisions:
        revision_id = row["artifact_revision_id"]
        add_lineage(
            "artifact_revision", revision_id, "compiled_into", "evidence_dossier", dossier_id
        )
        add_lineage("trace", compile_trace_id, "consumes", "artifact_revision", revision_id)


# ── 查询 ──────────────────────────────────────────────────────


@router.get("/sessions/{dossier_id}")
def get_session(dossier_id: str) -> dict[str, Any]:
    """查单个证据卷宗详情（含四层 + 两个视图）。"""
    dossier = repo.get_dossier(dossier_id)
    if dossier is None:
        raise HTTPException(status_code=404, detail=f"证据卷宗 {dossier_id} 不存在")
    return dossier


@router.get("/traces/{trace_id}/packs")
def list_trace_packs(trace_id: str) -> dict[str, Any]:
    """列出某 trace 的所有证据卷宗版本（版本号降序）。"""
    dossiers = repo.list_dossiers(trace_id)
    return {"packs": dossiers, "total": len(dossiers)}  # key 沿用 packs，前端兼容


@router.get("/consumable")
def list_consumable() -> dict[str, Any]:
    """列所有可评估的证据卷宗（status=ready，跨 trace，阶段 E 评估入口用）。

    返回每份可消费卷宗的摘要：dossier_id / trace_id / version / status /
    manifest 完整度 / created_at。评估页据此选卷宗启动评估。
    """
    rows = db.query_all(
        """SELECT pack_id, trace_id, owner_user_id, version, is_current, status,
                  provenance, compile_rule_version, manifest_json, failure_reason,
                  llm_calls_used, created_at, finished_at
           FROM evidence_dossiers
           WHERE status = 'ready'
           ORDER BY created_at DESC LIMIT 200""",
    )
    import json as _json
    items: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        item["dossier_id"] = item["pack_id"]  # 对外统一 dossier_id
        # manifest 摘要（完整度 + 契约覆盖）
        try:
            manifest = _json.loads(item.get("manifest_json") or "{}")
            item["completeness"] = manifest.get("completeness")
            matrix = manifest.get("contract_coverage_matrix") or {}
            item["contract_complete"] = matrix.get("complete")
        except (_json.JSONDecodeError, TypeError):
            item["completeness"] = None
            item["contract_complete"] = None
        item.pop("manifest_json", None)
        items.append(item)
    return {"dossiers": items, "total": len(items)}


@router.get("/traces/{trace_id}/current")
def get_current_pack(trace_id: str) -> dict[str, Any]:
    """查某 trace 的当前推荐版本（is_current=1）。无则 404。"""
    dossier = repo.get_current_dossier(trace_id)
    if dossier is None:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} 无当前证据卷宗")
    return dossier


# ── 停止 ──────────────────────────────────────────────────────


@router.post("/sessions/{dossier_id}/stop")
def stop_compile(dossier_id: str) -> dict[str, Any]:
    """取消运行中的编译（FR-006 / DEC-002）。

    立即标记 cancelling，后台 10 秒收敛。卷宗编译是 asyncio.to_thread 跑的同步
    函数，task.cancel 注入 CancelledError 后 to_thread 包装层会中断等待。
    """
    from contracts.cancel_state import HARD_STOP_DEADLINE_SECONDS

    dossier = repo.get_dossier(dossier_id)
    if dossier is None:
        raise HTTPException(status_code=404, detail=f"证据卷宗 {dossier_id} 不存在")
    if dossier["status"] not in ("pending", "compiling"):
        raise HTTPException(
            status_code=400,
            detail=f"证据卷宗状态为 {dossier['status']}，只有 pending/compiling 可停止",
        )

    # 立即标记 cancelling（DEC-002）。
    repo.update_dossier(dossier_id, status="compiling")  # 保持 compiling 语义，stop 标记靠 task

    task = _running_tasks.get(dossier_id)
    if task is not None and not task.done():
        task.cancel()

    # 后台收敛：等 task 结束（CancelledError 分支会标 cancelled）。
    import asyncio

    async def _converge() -> None:
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=HARD_STOP_DEADLINE_SECONDS)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        # _run_compile_bg 的 CancelledError 分支负责标 cancelled + recorder 收尾。
        # 超时后若仍未收敛，兜底标记。
        final = repo.get_dossier(dossier_id)
        if final and final["status"] not in ("cancelled", "failed", "ready", "partial"):
            repo.update_dossier(dossier_id, status="failed", failure_reason="取消超时", finished=True)

    asyncio.create_task(_converge())
    return {"status": "cancelling", "dossier_id": dossier_id}


# ── 证据回钻（权限校验） ─────────────────────────────────────


@router.get("/packs/{dossier_id}/drill/{evidence_id}")
def drill_evidence(dossier_id: str, evidence_id: str, request: Request) -> dict[str, Any]:
    """按证据 ID 回钻原始片段（受控回钻，权限校验）。

    evidence_id 格式 evt-{event_id}：从 event_payloads 加载原始事件。
    下游只能沿证据卷宗已提供的 ID 回钻，无 ID 的任意 trace 搜索不属于本能力。

    权限：只有卷宗的 owner（或超管）可回钻。证据卷宗冻结了产物正文，
    必须继承原 trace 归属，不能跨用户泄漏。
    """
    dossier = repo.get_dossier(dossier_id)
    if dossier is None:
        raise HTTPException(status_code=404, detail=f"证据卷宗 {dossier_id} 不存在")

    # 权限校验：当前用户必须是卷宗 owner 或超管
    current_user = getattr(request.state, "user_id", None)
    is_super = getattr(request.state, "is_super_admin", False)
    if current_user and not is_super and dossier["owner_user_id"] != "unknown" \
            and dossier["owner_user_id"] != current_user:
        raise HTTPException(
            status_code=403,
            detail="无权访问该证据卷宗（不属于当前用户）",
        )

    # 校验 evidence_id 在索引层内（受控回钻）
    index = dossier.get("index") or {}
    allowed_ids = set(index.get("evidence_ids", []))
    # evidence_id 格式 evt-{event_id}，索引层存的也是这个格式
    if evidence_id not in allowed_ids:
        raise HTTPException(
            status_code=404,
            detail=f"证据 ID {evidence_id} 不在证据卷宗索引中（受控回钻：只能沿卷宗内 ID 展开）",
        )

    # 从 evt-{event_id} 提取 event_id，加载原始事件。
    # event_id 在 payload_json 里（表无 event_id 列），按 trace 拉全部再筛。
    event_id = evidence_id
    if evidence_id.startswith("evt-"):
        event_id = evidence_id[4:]

    import json
    rows = db.query_all(
        "SELECT payload_json FROM event_payloads WHERE trace_id = ?",
        (dossier["trace_id"],),
    )
    payload = None
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if str(p.get("event_id", "")) == event_id:
            payload = p
            break
    if payload is None:
        raise HTTPException(status_code=404, detail=f"原始事件 {event_id} 不存在")

    return {
        "evidence_id": evidence_id,
        "trace_id": dossier["trace_id"],
        "event": payload,
    }


# ── 人工确认进证据编纂（REQ-20260802-211032）─────────────────────
# 产品负责人对"用户主动停止但有价值"的 cancelled+user_stop trace 发起确认：
# 确认后立即恢复半成品产物，该 trace 准入证据编纂。三层资格闸门同源放行。


class EvidenceOverrideApproveRequest(BaseModel):
    trace_id: str
    reason: str


@router.post("/evidence-override/approve")
async def approve_evidence_override_endpoint(
    req: EvidenceOverrideApproveRequest, request: Request
) -> dict[str, Any]:
    """确认一条用户主动停止的 trace 有价值，立即恢复半成品并准入证据编纂。

    仅产品负责人可调用（DEC-005/FR-005）。确认人身份从 SSO session 取，不接受请求体自报。
    RSK-003/NFR-001：approve_evidence_override 内含 recover_trace_artifacts（同步阻塞：
    load hydrated events + 线性化重建 + sha256 + 多表写入），大 trace 秒级耗时。
    用 asyncio.to_thread 丢线程池，避免阻塞事件循环（与 _run_compile_bg 同模式）。
    """
    approver = require_product_owner(request)
    try:
        result = await asyncio.to_thread(
            approve_evidence_override,
            req.trace_id, approver_user_id=approver, reason=req.reason,
        )
    except EvidenceOverrideError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return result


@router.post("/evidence-override/revoke/{trace_id}")
async def revoke_evidence_override_endpoint(
    trace_id: str, request: Request
) -> dict[str, Any]:
    """撤回人工确认。仅清退确认标记，已恢复产物与已编卷宗保留只读（DEC-003/FR-003）。

    仅产品负责人可调用。幂等：对未确认或已撤回的 trace 撤回无副作用。
    NFR-002：撤回人/时间记入 access_audit 审计链，可追溯。
    """
    from app.trace.access import audit_content_access

    revoker = require_product_owner(request)
    try:
        result = revoke_evidence_override(trace_id)
    except EvidenceOverrideError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    audit_content_access(request, "evidence_override_revoke", "trace", trace_id)
    return result


@router.get("/evidence-override/{trace_id}")
def get_evidence_override_state(trace_id: str, request: Request) -> dict[str, Any]:
    """查 trace 的人工确认状态（供前端判断是否显示确认/撤回按钮 + 撤回标注）。

    仅产品负责人可读取完整状态（含 approver/reason 审计字段，FR-005/NFR-002）。
    """
    require_product_owner(request)
    row = db.query_one(
        """SELECT evidence_override_approved, evidence_override_approver,
                  evidence_override_reason, evidence_override_approved_at,
                  evidence_override_revoked_at
           FROM runs WHERE trace_id=?""",
        (trace_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="trace 不存在")
    approved = bool(row.get("evidence_override_approved"))
    revoked_at = row.get("evidence_override_revoked_at")
    return {
        "trace_id": trace_id,
        "approved": approved and not revoked_at,
        "ever_approved": approved,
        "revoked_at": revoked_at,
        "approver": row.get("evidence_override_approver"),
        "reason": row.get("evidence_override_reason"),
        "approved_at": row.get("evidence_override_approved_at"),
    }


__all__ = ["router"]
