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


@router.post("/start", response_model=CompileStartResponse, status_code=202)
async def start_compile(req: CompileStartRequest) -> CompileStartResponse:
    """启动证据卷宗编译（异步）。立即返回 dossier_id，后台跑编译。

    幂等：
      - 同 trace + 同编译规则版本已有 ready/partial 卷宗 → 直接返回（不重复编译）
      - 同 trace + 同编译规则版本正在编译 → 直接返回该卷宗（不重复触发）
      - 否则新建 pending 卷宗 + 后台编译
    """
    trace_id = req.trace_id

    # 校验 trace 存在
    run_row = db.query_one(
        "SELECT owner_user_id, run_purpose FROM runs WHERE trace_id = ?",
        (trace_id,),
    )
    if run_row is None:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} 不存在")

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
        )

    # 幂等 2：同规则版本正在编译 → 直接返回
    active = repo.get_active_compiling(trace_id, COMPILE_RULE_VERSION)
    if active:
        return CompileStartResponse(
            dossier_id=active["dossier_id"], trace_id=trace_id, status=active["status"],
        )

    # 新建卷宗 + 后台编译
    dossier_id = repo.create_dossier(
        trace_id, owner_user_id,
        compile_rule_version=COMPILE_RULE_VERSION,
        provenance="compile_time_snapshot",  # 首期所有 trace 都是编译时快照
    )
    task = asyncio.create_task(_run_compile_bg(trace_id, dossier_id))
    _running_tasks[dossier_id] = task

    logger.info("证据卷宗编译启动: dossier=%s trace=%s", dossier_id, trace_id)
    return CompileStartResponse(dossier_id=dossier_id, trace_id=trace_id, status="started")


async def _run_compile_bg(trace_id: str, dossier_id: str) -> None:
    """后台执行证据卷宗编译。

    compiler.compile_dossier 是同步函数（内含 httpx 阻塞 LLM 调用），
    用 asyncio.to_thread 丢线程池，保持事件循环响应。
    """
    from app.dossier.compiler import compile_dossier
    try:
        await asyncio.to_thread(compile_dossier, trace_id, dossier_id)
    except asyncio.CancelledError:
        logger.info("证据卷宗编译 %s 被取消", dossier_id)
        repo.update_dossier(dossier_id, status="failed", failure_reason="用户取消", finished=True)
        raise
    except Exception as exc:
        logger.exception("证据卷宗编译 %s 后台异常", dossier_id)
        repo.update_dossier(dossier_id, status="failed", failure_reason=str(exc), finished=True)
    finally:
        _running_tasks.pop(dossier_id, None)


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
    """取消运行中的编译。"""
    dossier = repo.get_dossier(dossier_id)
    if dossier is None:
        raise HTTPException(status_code=404, detail=f"证据卷宗 {dossier_id} 不存在")
    if dossier["status"] not in ("pending", "compiling"):
        raise HTTPException(
            status_code=400,
            detail=f"证据卷宗状态为 {dossier['status']}，只有 pending/compiling 可停止",
        )
    task = _running_tasks.get(dossier_id)
    if task is not None and not task.done():
        task.cancel()
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
