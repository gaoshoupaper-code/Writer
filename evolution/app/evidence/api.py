"""证据编译 API（轨迹证据包 Trace Evidence Pack）。

端点：
  POST /evidence/start              启动编译（幂等：同规则版本已有 ready/partial 则直接返回）
  GET  /evidence/sessions/{pack_id} 查编译状态 + 包内容
  POST /evidence/sessions/{pack_id}/stop   取消编译
  GET  /evidence/packs/{pack_id}/drill/{evidence_id}  按证据 ID 回钻原始片段（权限校验）
  GET  /evidence/traces/{trace_id}/packs   列出 trace 的所有证据包版本

编译是纯后台计算（无 Agent 对话流），用 asyncio.create_task + asyncio.to_thread 跑。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import app.core.db as db
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.evidence import repo
from app.evidence.extractor import COMPILE_RULE_VERSION
from app.view.traces import load_trace_detail

logger = logging.getLogger("evolution.evidence.api")

router = APIRouter(prefix="/evidence", tags=["evidence"])

# pack_id → 后台编译 task。stop 端点靠它 cancel。
_running_tasks: dict[str, asyncio.Task] = {}


# ── 启动编译 ──────────────────────────────────────────────────


class CompileStartRequest(BaseModel):
    trace_id: str


class CompileStartResponse(BaseModel):
    pack_id: str
    trace_id: str
    status: str  # started | ready | partial | compiling


@router.post("/start", response_model=CompileStartResponse, status_code=202)
async def start_compile(req: CompileStartRequest) -> CompileStartResponse:
    """启动证据编译（异步）。立即返回 pack_id，后台跑编译。

    幂等：
      - 同 trace + 同编译规则版本已有 ready/partial 包 → 直接返回（不重复编译）
      - 同 trace + 同编译规则版本正在编译 → 直接返回该包（不重复触发）
      - 否则新建 pending 包 + 后台编译
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
            detail=f"trace {trace_id} 是进化端自观测 trace（run_purpose={run_purpose}），不能编译证据包。",
        )

    owner_user_id = run_row.get("owner_user_id") or "unknown"

    # 幂等 1：同规则版本已有可消费包 → 直接返回
    existing = repo.get_consumable_by_rule(trace_id, COMPILE_RULE_VERSION)
    if existing:
        return CompileStartResponse(
            pack_id=existing["pack_id"], trace_id=trace_id, status=existing["status"],
        )

    # 幂等 2：同规则版本正在编译 → 直接返回
    active = repo.get_active_compiling(trace_id, COMPILE_RULE_VERSION)
    if active:
        return CompileStartResponse(
            pack_id=active["pack_id"], trace_id=trace_id, status=active["status"],
        )

    # 新建包 + 后台编译
    pack_id = repo.create_pack(
        trace_id, owner_user_id,
        compile_rule_version=COMPILE_RULE_VERSION,
        provenance="compile_time_snapshot",  # 首期所有 trace 都是编译时快照
    )
    task = asyncio.create_task(_run_compile_bg(trace_id, pack_id))
    _running_tasks[pack_id] = task

    logger.info("证据编译启动: pack=%s trace=%s", pack_id, trace_id)
    return CompileStartResponse(pack_id=pack_id, trace_id=trace_id, status="started")


async def _run_compile_bg(trace_id: str, pack_id: str) -> None:
    """后台执行证据编译。

    compiler.compile_evidence 是同步函数（内含 httpx 阻塞 LLM 调用），
    用 asyncio.to_thread 丢线程池，保持事件循环响应。
    """
    from app.evidence.compiler import compile_evidence
    try:
        await asyncio.to_thread(compile_evidence, trace_id, pack_id)
    except asyncio.CancelledError:
        logger.info("证据编译 %s 被取消", pack_id)
        repo.update_pack(pack_id, status="failed", failure_reason="用户取消", finished=True)
        raise
    except Exception as exc:
        logger.exception("证据编译 %s 后台异常", pack_id)
        repo.update_pack(pack_id, status="failed", failure_reason=str(exc), finished=True)
    finally:
        _running_tasks.pop(pack_id, None)


# ── 查询 ──────────────────────────────────────────────────────


@router.get("/sessions/{pack_id}")
def get_session(pack_id: str) -> dict[str, Any]:
    """查单个证据包详情（含四层 + 两个视图）。"""
    pack = repo.get_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"证据包 {pack_id} 不存在")
    return pack


@router.get("/traces/{trace_id}/packs")
def list_trace_packs(trace_id: str) -> dict[str, Any]:
    """列出某 trace 的所有证据包版本（版本号降序）。"""
    packs = repo.list_packs(trace_id)
    return {"packs": packs, "total": len(packs)}


@router.get("/traces/{trace_id}/current")
def get_current_pack(trace_id: str) -> dict[str, Any]:
    """查某 trace 的当前推荐版本（is_current=1）。无则 404。"""
    pack = repo.get_current_pack(trace_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} 无当前证据包")
    return pack


# ── 停止 ──────────────────────────────────────────────────────


@router.post("/sessions/{pack_id}/stop")
def stop_compile(pack_id: str) -> dict[str, Any]:
    """取消运行中的编译。"""
    pack = repo.get_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"证据包 {pack_id} 不存在")
    if pack["status"] not in ("pending", "compiling"):
        raise HTTPException(
            status_code=400,
            detail=f"证据包状态为 {pack['status']}，只有 pending/compiling 可停止",
        )
    task = _running_tasks.get(pack_id)
    if task is not None and not task.done():
        task.cancel()
    return {"status": "cancelling", "pack_id": pack_id}


# ── 证据回钻（权限校验） ─────────────────────────────────────


@router.get("/packs/{pack_id}/drill/{evidence_id}")
def drill_evidence(pack_id: str, evidence_id: str, request: Request) -> dict[str, Any]:
    """按证据 ID 回钻原始片段（受控回钻，权限校验）。

    evidence_id 格式 evt-{event_id}：从 event_payloads 加载原始事件。
    下游只能沿证据包已提供的 ID 回钻，无 ID 的任意 trace 搜索不属于本能力。

    权限：只有 pack 的 owner（或超管）可回钻。证据包冻结了产物正文，
    必须继承原 trace 归属，不能跨用户泄漏。
    """
    pack = repo.get_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"证据包 {pack_id} 不存在")

    # 权限校验：当前用户必须是 pack owner 或超管
    current_user = getattr(request.state, "user_id", None)
    is_super = getattr(request.state, "is_super_admin", False)
    if current_user and not is_super and pack["owner_user_id"] != "unknown" \
            and pack["owner_user_id"] != current_user:
        raise HTTPException(
            status_code=403,
            detail="无权访问该证据包（不属于当前用户）",
        )

    # 校验 evidence_id 在索引层内（受控回钻）
    index = pack.get("index") or {}
    allowed_ids = set(index.get("evidence_ids", []))
    # evidence_id 格式 evt-{event_id}，索引层存的也是这个格式
    if evidence_id not in allowed_ids:
        raise HTTPException(
            status_code=404,
            detail=f"证据 ID {evidence_id} 不在证据包索引中（受控回钻：只能沿包内 ID 展开）",
        )

    # 从 evt-{event_id} 提取 event_id，加载原始事件
    event_id = evidence_id
    if evidence_id.startswith("evt-"):
        event_id = evidence_id[4:]

    row = db.query_one(
        "SELECT payload_json FROM event_payloads WHERE trace_id = ? AND event_id = ?",
        (pack["trace_id"], event_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"原始事件 {event_id} 不存在")

    import json
    payload = json.loads(row["payload_json"])
    return {
        "evidence_id": evidence_id,
        "trace_id": pack["trace_id"],
        "event": payload,
    }


__all__ = ["router"]
