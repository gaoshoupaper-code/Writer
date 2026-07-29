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
from app.view.traces import load_trace_detail
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

    # 防自观测：不能编译进化端自观测 trace
    run_purpose = run_row.get("run_purpose") or "user_generation"
    if run_purpose in ("evolution_eval", "evolution_evolve"):
        raise HTTPException(
            status_code=400,
            detail=f"trace {trace_id} 是进化端自观测 trace（run_purpose={run_purpose}），不能编译证据卷宗。",
        )

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
    dossier_id = repo.create_dossier(
        trace_id, owner_user_id,
        compile_rule_version=COMPILE_RULE_VERSION,
        provenance=(
            "trace_time"
            if db.query_one(
                "SELECT 1 AS found FROM artifact_revisions WHERE producer_trace_id=? LIMIT 1",
                (trace_id,),
            )
            else "compile_time_snapshot"
        ),
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
    try:
        result = await asyncio.to_thread(compile_dossier, trace_id, dossier_id)
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
        "SELECT artifact_revision_id FROM artifact_revisions WHERE producer_trace_id=?",
        (source_trace_id,),
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


__all__ = ["router"]
