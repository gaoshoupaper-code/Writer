"""evolve API —— 进化触发 + 查询 + SSE + 发版/丢弃（三功能解耦，决策 S8/S9）。

端点：
  POST /api/evolve/start                        触发进化（强前置：trace 必须已评估，S8）
  GET  /api/evolve/sessions                     session 列表（最新在前）
  GET  /api/evolve/sessions/{id}                单 session 详情
  GET  /api/evolve/sessions/{id}/stream         SSE 实时事件流
  POST /api/evolve/sessions/{id}/publish        发版（S9/S12：git commit + bootstrap config + snapshot）
  POST /api/evolve/sessions/{id}/discard        丢弃（S9：git reset 回 production + 状态推进）

执行模型（D3/D4：trace 统一接管 SSE）：
  start 时注入 recorder 到 ctx → 后台 task 跑进化驱动器 → recorder 产 trace 事件 →
  SSE 从 recorder 队列消费推前端。SessionEvents 已删除。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from contracts.cancel_state import HARD_STOP_DEADLINE_SECONDS, is_terminal
from app.eval_agent import repo as eval_repo
from app.core import db
from app.evolve import db as ev_db
from app.evolve.agent.agent import run_evolve_session
from app.evolve.ctx import (
    ACTIVE_STATUSES,
    STATUS_CONVERSING,
    STATUS_FINALIZING,
    STATUS_RUNNING,
    EvolveContext,
)
from app.trace.recorder import EvolutionTraceRecorder
from app.trace.facts import (
    ConsumptionRejected,
    append_release_event,
    require_sealed_evaluation_dossier,
)

logger = logging.getLogger("evolution.evolve.api")

router = APIRouter(tags=["evolve"])

# session_id → 后台进化 task。stop 端点靠它 cancel 正在跑的 Agent。
# 原先用 FastAPI BackgroundTasks.add_task 不持有 task 引用，外部无法取消；
# 改用 asyncio.create_task 后存这里，stop 才能调 task.cancel()。
_running_tasks: dict[str, asyncio.Task] = {}


def get_recorder() -> EvolutionTraceRecorder | None:
    """获取全局 recorder 实例（main.py lifespan 注入到 app.state）。"""
    from app.main import app
    return getattr(app.state, "trace_recorder", None)


class EvolveStartRequest(BaseModel):
    """进化启动请求（阶段 D：按评估卷宗启动，永久绑定）。"""

    eval_dossier_id: str  # 必填：要进化的评估卷宗 id（须为 sealed 完整态）
    # CON-010 / DEC-012 / AC-015：来源评估运行已取消的 sealed 卷宗，必须由授权用户
    # 显式确认才能人工提交。系统永不自动调度取消来源卷宗。
    confirmed_cancel_origin: bool = False


class EvolveStartResponse(BaseModel):
    session_id: str
    trace_id: str
    eval_dossier_id: str  # 永久绑定的评估卷宗
    status: str  # started


# ── 触发 ────────────────────────────────────────────────────


@router.post("/evolve/start", response_model=EvolveStartResponse, status_code=202)
async def evolve_start(
    req: EvolveStartRequest,
) -> EvolveStartResponse:
    """触发一次进化（方案→执行两阶段，单体兼容入口）。

    阶段 D（2026-07-27）：按评估卷宗启动，永久绑定（需求 §42）。
    进化 Agent 只读评估卷宗（结论 + 引用的冻结证据），不读原始 trace / 完整证据卷宗。
    """
    eval_dossier = _resolve_eval_dossier(
        req.eval_dossier_id, confirmed_cancel_origin=req.confirmed_cancel_origin
    )
    active = _find_active_session()
    if active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"当前有未结束的进化会话（session {active['session_id']}，状态 {active['status']}），"
                f"请先发布/丢弃/取消后再启动新进化"
            ),
        )

    session_id, ctx = _prepare_evolve_session(req.eval_dossier_id, eval_dossier)

    # 后台跑进化驱动器（单体兼容）
    task = asyncio.create_task(_run_evolve_bg(ctx, eval_dossier["trace_id"]))
    _running_tasks[session_id] = task

    logger.info(
        "进化 session 启动（单体）: session=%s evd=%s trace=%s",
        session_id, req.eval_dossier_id, eval_dossier["trace_id"],
    )
    return EvolveStartResponse(
        session_id=session_id, trace_id=eval_dossier["trace_id"],
        eval_dossier_id=req.eval_dossier_id, status="started",
    )


@router.post("/evolve/start-converse", response_model=EvolveStartResponse, status_code=202)
async def evolve_start_converse(req: EvolveStartRequest) -> EvolveStartResponse:
    """触发对话式共创进化（Phase 3，决策 T2/T10）。

    阶段 D：按评估卷宗启动，永久绑定。内部走 inspect round（探查 + Agent 开场白），
    跑完后 status 自动转 conversing，等用户在对话区发消息（POST /messages）。
    """
    eval_dossier = _resolve_eval_dossier(
        req.eval_dossier_id, confirmed_cancel_origin=req.confirmed_cancel_origin
    )
    active = _find_active_session()
    if active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"当前有未结束的进化会话（session {active['session_id']}，状态 {active['status']}），"
                f"请先发布/丢弃/取消后再启动新进化"
            ),
        )

    session_id, ctx = _prepare_evolve_session(req.eval_dossier_id, eval_dossier)

    # 后台跑 inspect round（探查 + 开场白 → 转 conversing）
    from app.evolve.agent.agent import run_inspect_round
    task = asyncio.create_task(_run_round_bg(ctx, run_inspect_round, eval_dossier["trace_id"]))
    _running_tasks[session_id] = task

    logger.info(
        "进化 session 启动（对话式）: session=%s evd=%s trace=%s",
        session_id, req.eval_dossier_id, eval_dossier["trace_id"],
    )
    return EvolveStartResponse(
        session_id=session_id, trace_id=eval_dossier["trace_id"],
        eval_dossier_id=req.eval_dossier_id, status="started_converse",
    )


def _prepare_evolve_session(
    eval_dossier_id: str, eval_dossier: dict[str, Any]
) -> tuple[str, EvolveContext]:
    """创建进化会话 + 构建上下文 + 永久绑定评估卷宗（单体/对话式共用）。

    bound_eval_dossier_id 写入 evolve_sessions，会话创建后永久不变（需求 §42）。
    """
    if get_recorder() is None:
        raise HTTPException(status_code=503, detail={
            "message": "Trace recorder unavailable; evolution was not started",
            "integrity_status": "incomplete",
            "missing_fields": ["trace_recorder"],
        })
    session_id = uuid.uuid4().hex[:12]
    ev_db.create_session(session_id, case_id="")
    ctx = _build_evolve_ctx(session_id, eval_dossier)
    # 永久绑定评估卷宗（不可变）
    ev_db.update_session(session_id, bound_eval_dossier_id=eval_dossier_id)
    return session_id, ctx


async def _run_round_bg(
    ctx: EvolveContext,
    round_fn,
    *args,
) -> None:
    """通用后台 round 执行器（决策 T2 按需触发）。

    与 _run_evolve_bg 对称，但跑的是任意 round 函数（inspect/converse/finalize）。
    round 函数自己负责状态推进 + recorder 收尾，本函数只做异常兜底 + task 注册表清理。

    Args:
        ctx: 进化上下文
        round_fn: round 函数（run_inspect_round / run_converse_round / run_finalize_round）
        *args: 传给 round_fn 的位置参数（如 trace_id / user_message）
    """
    try:
        result = await round_fn(ctx, *args)
        # cancelled 是用户停止的合法终态，不算失败
        if result.get("status") not in (
            "done", "conversing", "pending_review", "cancelled", None,
        ):
            ev_db.update_session(ctx.session_id, status="failed")
    except asyncio.CancelledError:
        logger.info("进化 session %s round %s 被取消", ctx.session_id, round_fn.__name__)
        # round 函数自己处理 cancelled；这里兜底（取消在进入 round 前命中）
        ev_db.update_session(ctx.session_id, status="cancelled")
        raise
    except Exception as e:
        logger.exception("进化 session %s round %s 异常", ctx.session_id, round_fn.__name__)
        ev_db.update_session(ctx.session_id, status="failed")
    finally:
        _running_tasks.pop(ctx.session_id, None)


def _find_active_session() -> dict[str, Any] | None:
    """查是否有活跃的进化 session（决策 G 单会话锁）。

    活跃 = status ∈ ACTIVE_STATUSES（running/conversing/finalizing/pending_review）。
    返回 session dict（含 session_id + status），无活跃返回 None。
    """
    sessions = ev_db.list_sessions(limit=50)
    for s in sessions:
        if isinstance(s, dict) and s.get("status") in ACTIVE_STATUSES:
            return s
    return None


def _resolve_eval_dossier(
    eval_dossier_id: str, *, confirmed_cancel_origin: bool = False,
    _skip_cancel_check: bool = False,
) -> dict[str, Any]:
    """校验评估卷宗存在 + 已封存（sealed）+ 完整（需求 §42 进化输入边界）。

    阶段 D：进化只接受已封存的评估卷宗。旧链路（按 trace_id 查评估）废弃。
    CON-010 / DEC-012 / AC-015：来源评估运行已取消的 sealed 卷宗，必须由授权用户
    显式确认（confirmed_cancel_origin=True）才能人工提交。
    _skip_cancel_check 仅用于会话恢复路径（启动时已检查过，恢复无需重复确认）。
    Returns:
        评估卷宗 dict（含 findings/frozen_evidence/scores/report_md）。
    Raises:
        HTTPException: 卷宗不存在 / 未封存 / 不完整 / 取消来源未确认。
    """
    try:
        row = require_sealed_evaluation_dossier(eval_dossier_id)
    except ConsumptionRejected as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "integrity_status": exc.integrity_status,
                "missing_fields": list(exc.missing_fields),
            },
        ) from exc

    # CON-010 / AC-015：检测评估来源 trace 是否 cancelled。
    # 会话恢复路径（_skip_cancel_check）启动时已检查过，跳过。
    if not _skip_cancel_check:
        evaluation_trace_id = row.get("evaluation_trace_id")
        if evaluation_trace_id:
            eval_run = db.query_one(
                "SELECT status FROM runs WHERE trace_id=?", (evaluation_trace_id,)
            )
            if eval_run and eval_run.get("status") == "cancelled":
                if not confirmed_cancel_origin:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": (
                                f"评估卷宗 {eval_dossier_id} 的来源评估运行已取消（cancelled）。"
                                "取消来源的卷宗必须由您确认了解其来源已取消、可能影响进化结论"
                                "后才能人工提交。"
                            ),
                            "cancel_origin_confirmation_required": True,
                            "source_trace_id": evaluation_trace_id,
                            "source_status": "cancelled",
                        },
                    )
                db.execute(
                    """INSERT INTO cancel_origin_submissions
                       (source_trace_id, source_status, dossier_id, target_downstream,
                        submitted_by, confirmed_at)
                       VALUES (?, 'cancelled', ?, 'evolution', 'api', ?)""",
                    (evaluation_trace_id, eval_dossier_id, datetime.now(UTC).isoformat()),
                )
                logger.info(
                    "取消来源评估卷宗 %s 经人工确认进入进化: source_trace=%s",
                    eval_dossier_id, evaluation_trace_id,
                )

    import json as _json
    dossier: dict[str, Any] = dict(row)
    for col in ("conclusions_json", "findings_json", "positive_patterns_json",
                "scores_json", "frozen_evidence_json"):
        raw = dossier.get(col)
        if raw:
            try:
                dossier[col[:-5]] = _json.loads(raw)  # 去 _json 后缀
            except (_json.JSONDecodeError, TypeError):
                dossier[col[:-5]] = None
        else:
            dossier[col[:-5]] = None

    findings = dossier.get("findings")
    if not findings or not isinstance(findings, list):
        raise HTTPException(
            status_code=400,
            detail=f"评估卷宗 {eval_dossier_id} 无结构化 findings，不能启动进化",
        )
    return dossier


def _build_evolve_ctx(session_id: str, eval_dossier: dict[str, Any]) -> EvolveContext:
    """构建进化上下文：评估卷宗成为唯一业务证据输入（阶段 D）。

    单体 /start 和对话式 /start-converse 共用。
    评估卷宗含 findings + 冻结证据片段 + scores + report_md，进化不读原始 trace/完整证据卷宗。
    """
    trace_id = eval_dossier["trace_id"]
    ctx = EvolveContext(session_id=session_id)
    ctx.recorder = get_recorder()
    ctx.trace_id = trace_id  # 仅用于自观测录像归属，不作为业务证据输入
    ctx.origin_layer = _resolve_origin_layer(trace_id)
    # 评估卷宗是进化的唯一业务证据输入（需求 §22）
    ctx.eval_dossier = eval_dossier
    ctx.eval_dossier_id = eval_dossier["dossier_id"]
    # eval_snapshot 保留向后兼容（read_eval_report 工具读它）—— 从评估卷宗组装
    ctx.eval_snapshot = {
        "eval_dossier_id": eval_dossier["dossier_id"],
        "trace_id": trace_id,
        "scores": eval_dossier.get("scores"),
        "findings": eval_dossier.get("findings"),
        "report_md": eval_dossier.get("report_md"),
    }
    # eval_ref 关联评估尝试（兼容旧字段）
    ev_db.update_session(session_id, eval_ref=eval_dossier.get("eval_attempt_id"))
    return ctx


def _resolve_origin_layer(trace_id: str) -> str | None:
    """查 trace 所属的数据集层（数据闭环 F1，golden|growing）。

    通过 manual_tests.origin_layer 反查（测试发起时写入）。
    非 benchmark/测试 trace（如用户原始 trace）返回 None。
    """
    row = db.query_one(
        "SELECT origin_layer FROM manual_tests WHERE trace_id=? AND origin_layer IS NOT NULL LIMIT 1",
        (trace_id,),
    )
    return row["origin_layer"] if row else None


async def _run_evolve_bg(ctx: EvolveContext, trace_id: str) -> None:
    """后台执行进化驱动器（方案→执行两阶段）。

    D3/D4：trace 终态（complete/fail_run）已在 run_evolve_session 内处理。
    """
    try:
        result = await run_evolve_session(ctx, trace_id)
        # cancelled 是用户主动停止的合法终态，不算失败。
        if result["status"] not in ("done", "cancelled"):
            ev_db.update_session(ctx.session_id, status="failed")
    except asyncio.CancelledError:
        # task.cancel() 触发；run_evolve_session 内部已处理状态推进，
        # 但若取消在进入 session 函数前命中，这里兜底标 cancelled。
        logger.info("进化 session %s 在后台被取消", ctx.session_id)
        ev_db.update_session(ctx.session_id, status="cancelled")
        raise
    except Exception as e:
        logger.exception("进化 session %s 后台执行异常", ctx.session_id)
        ev_db.update_session(ctx.session_id, status="failed")
    finally:
        _running_tasks.pop(ctx.session_id, None)


# ── 查询 ────────────────────────────────────────────────────


@router.get("/evolve/system-prompt")
def get_system_prompt() -> dict[str, Any]:
    """返回进化 Agent 的静态架构蓝图（决策 F/Q/R）。

    前端「架构蓝图」Tab 的数据源——打开进化页即可调用，不依赖任何 session。
    返回 STATIC_BLUEPRINT（7 段全景 + 角色定位 + 能力边界 + 对创作 Agent 的理解）。
    动态注入部分（session_id / eval_summary / reflections / memory）不在此返回。

    Returns:
        {blueprint: <markdown 字符串>, version: <服务版本>}
    """
    from app.evolve.agent.prompt import STATIC_BLUEPRINT
    return {
        "blueprint": STATIC_BLUEPRINT,
        "version": "v0.2.24",
    }


@router.get("/evolve/sessions/{session_id}/messages")
def get_messages(session_id: str, after_seq: int | None = None) -> dict[str, Any]:
    """列出 session 的对话消息（决策 H/T6，前端刷新恢复）。

    旧会话（无 evolve_messages 记录）返回空列表——前端据此识别"旧版会话"
    并提示用户（决策 S）。

    Args:
        session_id: session id
        after_seq: 增量拉取——只返回 seq > after_seq 的消息；None = 全量
    Returns:
        {messages: [EvolveMessage, ...]}
    """
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")

    from app.evolve.evolve_repo import EvolveMessagesRepo
    messages = EvolveMessagesRepo.list_by_session(session_id, after_seq=after_seq)
    return {"messages": messages}


@router.get("/evolve/sessions/{session_id}/points")
def get_points(session_id: str) -> dict[str, Any]:
    """列出 session 的进化点清单（决策 M/T7，右侧浮窗数据源）。

    返回全部进化点（含 proposed/accepted/rejected 状态），按 seq 升序。
    前端浮窗据此渲染状态图标 + 双向高亮联动（决策 N）。

    Returns:
        {points: [EvolvePoint, ...], accepted_count: <int>}
    """
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")

    from app.evolve.evolve_repo import EvolvePointsRepo
    points = EvolvePointsRepo.list_by_session(session_id)
    accepted_count = sum(1 for p in points if p.get("status") == "accepted")
    return {"points": points, "accepted_count": accepted_count}


@router.get("/evolve/sessions")
def list_sessions(limit: int = 50) -> dict[str, Any]:
    """列出进化 session（最新在前）。"""
    sessions = ev_db.list_sessions(limit=limit)
    return {"sessions": sessions, "total": len(sessions)}


@router.get("/evolve/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    """查单个 session 详情（含内联的 design_doc/change_log/eval_snapshot）。

    审查视图所需数据全部内联到这里，前端一次请求拿全：
      - design_doc：读盘 design_doc.md（解析 front matter → {meta, body}）
      - change_log：读盘 change_log.md（解析 front matter → {meta, body}）
      - eval_snapshot：通过 eval_ref 查 evaluation_sessions，取 findings + scores

    读盘/查询失败时对应字段设 null（R8：残缺不崩，前端走残缺渲染）。
    """
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")

    # 内联 design_doc（方案子代理产出）
    session["design_doc"] = _try_read_doc(session.get("design_doc_path"))

    # 内联 change_log（执行子代理产出）
    session["change_log"] = _try_read_doc(session.get("change_log_path"))

    # 内联关联评估的 findings + scores（审查证据来源）
    session["eval_snapshot"] = _try_load_eval_snapshot(session.get("eval_ref"))

    from app.versioning import registry_repo
    session["release_candidate"] = registry_repo.get_candidate_by_session(session_id)

    return session


def _try_read_doc(path: str | None) -> dict[str, Any] | None:
    """读盘一个 markdown+YAML 文档，返回 {meta, body}。失败返回 None。

    复用 docs._load_doc 的解析逻辑（front matter 分割）。
    """
    if not path:
        return None
    try:
        from app.evolve.docs import _load_doc
        meta, body = _load_doc(path)
        return {"meta": meta, "body": body}
    except FileNotFoundError:
        logger.warning("文档不存在: %s", path)
        return None
    except Exception:
        logger.exception("文档解析失败: %s", path)
        return None


def _try_load_eval_snapshot(eval_ref: str | None) -> dict[str, Any] | None:
    """查关联评估的 findings + scores（审查证据来源）。

    不带 report_md（太长，审查视图只需 finding 级证据 + 分数对比）。
    """
    if not eval_ref:
        return None
    try:
        ev = eval_repo.get_session(eval_ref)
        if not ev:
            return None
        return {
            "eval_id": ev.get("eval_id"),
            "trace_id": ev.get("trace_id"),
            "findings": ev.get("findings"),
            "scores": ev.get("scores"),
        }
    except Exception:
        logger.exception("查评估快照失败: eval_ref=%s", eval_ref)
        return None


# ── 停止 ────────────────────────────────────────────────────


@router.post("/evolve/sessions/{session_id}/stop")
def stop_session(session_id: str) -> dict[str, Any]:
    """手动停止运行中的进化 session（FR-006 / NFR-001 / DEC-002）。

    立即标记 cancelling 并返回（DEC-002），后台 asyncio task 在 10 秒时限内
    收敛到 cancelled。

    已知边界：Agent 若停在改源码中途，harnesses/current/ 下可能留脏文件，
    本端点不清理（由用户手动 stash / 重置）。
    """
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")
    current = session.get("status")
    if is_terminal(current):
        raise HTTPException(
            status_code=409,
            detail=f"session 状态为 {current}，已终态，无需停止",
        )
    # 可停止的非终态：running / conversing / finalizing（pending_review 走 publish/discard）。
    stoppable = {STATUS_RUNNING, STATUS_CONVERSING, STATUS_FINALIZING}
    if current not in stoppable:
        raise HTTPException(
            status_code=400,
            detail=f"session 状态为 {current}，只有 running/conversing/finalizing 可停止",
        )

    # 立即标记 cancelling（DEC-002）。
    ev_db.update_session(session_id, status="cancelling")

    # 后台 asyncio task 做 10 秒硬终止收敛。
    task = _running_tasks.get(session_id)
    asyncio.create_task(_converge_evolve_cancel(session_id, task))
    return {"status": "cancelling", "session_id": session_id}


async def _converge_evolve_cancel(session_id: str, task: asyncio.Task | None) -> None:
    """进化取消收敛：task.cancel → 等 10s → recorder 强制收敛 → 标 cancelled/cancel_timeout。

    CON-003/EDGE-007：超时未退出标 cancel_timeout（诚实告警，不谎报 cancelled）。
    """
    recorder = get_recorder()
    trace_id_self = recorder.get_trace_id_by_session(session_id) if recorder else None

    if task is not None and not task.done():
        task.cancel()

    # CON-003 真实停止确认：以 task.done() 为准——deadline 后仍未退出才 cancel_timeout。
    converged_in_time = True
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=HARD_STOP_DEADLINE_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
    if task is not None and not task.done():
        converged_in_time = False  # deadline 后仍未退出 → cancel_timeout

    if recorder and trace_id_self:
        recorder.cancel_run(trace_id_self, reason="user_stop")

    final_status = "cancelled" if converged_in_time else "cancel_timeout"
    ev_db.update_session(session_id, status=final_status)
    logger.info("进化 session %s 取消收敛完成 status=%s", session_id, final_status)


# ── SSE 实时流 ──────────────────────────────────────────────


# ── trace 稳定性重构：Pull 模式事件流（替代 SSE，设计 20260720_203000）──


class EvolveEventsSinceResponse(BaseModel):
    """进化 session 事件游标拉取响应（Pull 主导）。"""
    frames: list[dict[str, Any]]   # 从 run_meta 派生的 step/log/phase/proposal/finalizing/message_updated 帧
    max_seq: int                    # 本次返回的最大 sequence（前端下次 since_seq）；无事件时 = since_seq
    has_more: bool                  # 是否还有更多事件未拉（罕见，重构后事件密度低）
    session_status: str             # session 当前状态（running/conversing/...），前端据此判断是否继续轮询


@router.get("/evolve/sessions/{session_id}/events/since", response_model=EvolveEventsSinceResponse)
def get_session_events_since(
    session_id: str,
    since_seq: int = Query(0, ge=0, description="返回 sequence > since_seq 的事件"),
    limit: int = Query(500, ge=1, le=1000, description="单次返回上限"),
) -> EvolveEventsSinceResponse:
    """按 sequence 游标拉取进化 session 的事件帧（trace 重构 20260720_154825）。

    实现：从 evolve_sessions.self_trace_id 反查 trace_id → 查 event_payloads 表的
    run_meta 事件 → 用 _trace_event_to_sse 派生成 phase/proposal/finalizing/
    message_updated/step/log 帧。

    重构变更（D1/D3）：
      - 不再有 model_stream token 流帧（每 token 一行的污染源已移除）
      - 新增 message_updated 帧：Agent 消息已落 evolve_messages，前端据此调
        GET /messages 拉权威消息（按 after_seq 增量）
      - 事件密度大幅降低（每轮 LLM/工具只产 1-2 个 run_meta，而非 N 个 token）
      - 前端轮询间隔可放宽到 2s（无 token 流实时性要求）
    """
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")

    self_trace_id = session.get("self_trace_id")
    frames: list[dict[str, Any]] = []
    max_seq = since_seq

    if self_trace_id:
        rows = db.query_all(
            """SELECT sequence, payload_json FROM event_payloads
               WHERE trace_id=? AND sequence>?
               ORDER BY sequence LIMIT ?""",
            (self_trace_id, since_seq, limit + 1),
        )
        has_more = len(rows) > limit
        rows = rows[:limit]

        for r in rows:
            seq = r["sequence"]
            if seq > max_seq:
                max_seq = seq
            try:
                from app.core.models import TraceLogEvent
                evt = TraceLogEvent.model_validate(json.loads(r["payload_json"]))
                frame = _trace_event_to_sse(evt)
                if frame:
                    frame["_seq"] = seq
                    frames.append(frame)
            except Exception:
                # 单条解析失败不阻断其它事件。
                continue
    else:
        has_more = False

    return EvolveEventsSinceResponse(
        frames=frames,
        max_seq=max_seq,
        has_more=has_more,
        session_status=session.get("status", "running"),
    )


def _trace_event_to_sse(event: Any) -> dict[str, Any] | None:
    """trace 事件 → 前端 Pull 帧派生（trace 重构 20260720_154825）。

    重构后只派生以下帧（移除了 sse_frame 桥接 / model_stream）：
      - tool="phase"            → {type:"phase", phase}（阶段切换）
      - tool="proposal"         → {type:"proposal", ...}（浮窗进化点状态变更）
      - tool="finalizing"       → {type:"finalizing", ...}（落地进度）
      - tool="message_updated"  → {type:"message_updated"}（前端据此调 loadMessages）
      - 含 message 字段（无 tool）→ {type:"log", message}（思考日志）
      - 含 tool 字段（其他）      → {type:"step", **data}（业务步骤，向后兼容）

    设计变更（D1/D3）：
      - 不再有 sse_frame 桥接：token 流不入 trace，事件数与 span 数对齐
      - 新增 message_updated 帧：消息已落 evolve_messages，前端拉权威存储
      - 前端不再维护临时消息 state，全部走 loadMessages 增量拉
    """
    if event.type != "run_meta" or not event.input:
        return None
    data = event.input if isinstance(event.input, dict) else {}
    tool = data.get("tool", "")

    # trace 重构：消息更新通知（前端调 loadMessages 增量拉）
    if tool == "message_updated":
        return {"type": "message_updated"}

    # 阶段切换事件
    if tool == "phase":
        phase = data.get("phase")
        if phase:
            return {"type": "phase", "phase": phase}
        return None

    # 进化点状态变更（决策 B/M 浮窗实时同步）
    if tool == "proposal":
        return {
            "type": "proposal",
            "action": data.get("action"),
            "point_id": data.get("point_id"),
            "seq": data.get("seq"),
            "target": data.get("target"),
            "chosen_option": data.get("chosen_option"),
        }

    # 落地进度事件（决策 W）
    if tool == "finalizing":
        return {
            "type": "finalizing",
            "event": data.get("status"),  # edit/validate/change_log
            "target": data.get("target"),
            "result": data.get("result"),
        }

    # 旧协议：log + step（保留向后兼容）
    if "message" in data and not tool:
        return {"type": "log", "message": data["message"]}
    if tool:
        return {"type": "step", **data}
    return None


# ── 发版 / 丢弃（Phase 4，S9/S12）────────────────────────────


@router.post("/evolve/sessions/{session_id}/publish")
def publish_session(session_id: str, request: Request) -> dict[str, Any]:
    """两阶段发版：先冻结 candidate，再凭同一身份的 snapshot 晋升。"""
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")
    if session.get("status") != "pending_review":
        raise HTTPException(
            status_code=400,
            detail=f"session 状态为 {session.get('status')}，只有 pending_review 可发版",
        )

    from app.core import git_ops
    from app.versioning import registry_repo
    from app.versioning.release_gate import probe_candidate, validate_candidate_snapshot
    from app.versioning.snapshot_publisher import reload_executor

    release_id = f"release-{session_id}"
    actor_user_id = getattr(request.state, "user_id", None)
    candidate = registry_repo.get_version_by_session(session_id)

    try:
        if candidate is None:
            version = registry_repo.next_version_number()
            source_commit = git_ops.commit_candidate(
                f"冻结 Harness candidate v{version}: session={session_id}",
                required_paths=("middleware/artifact_snapshot.py",),
            )
            probe = probe_candidate(source_commit)
            candidate = registry_repo.create_candidate(
                version=version,
                commit_hash=source_commit,
                change_summary=f"进化 session {session_id} 产出的改动",
                source_session=session_id,
                probe_identity=probe.get("runtime_identity") or {},
            )
            git_ops.commit_registry_and_push(
                f"注册 Harness candidate v{version}: session={session_id}"
            )
            candidate_id = f"harness-version-{version}"
            append_release_event(
                release_id=release_id,
                status="committed",
                candidate_id=candidate_id,
                actor_user_id=actor_user_id,
            )
            return {
                "status": "candidate_pending_snapshot",
                "release_id": release_id,
                "snapshot_version": version,
                "source_commit": source_commit,
            }

        version = candidate["version"]
        source_commit = candidate["commit_hash"]
        candidate_id = f"harness-version-{version}"
        release_fact = db.query_one(
            """SELECT status FROM release_events_v2 WHERE release_id=?
               ORDER BY rowid DESC LIMIT 1""",
            (release_id,),
        )
        if release_fact is None:
            git_ops.commit_registry_and_push(
                f"注册 Harness candidate v{version}: session={session_id}"
            )
            append_release_event(
                release_id=release_id,
                status="committed",
                candidate_id=candidate_id,
                actor_user_id=actor_user_id,
            )
            release_status = "committed"
        else:
            release_status = release_fact["status"]
        if release_status == "activated" and candidate.get("status") == "production":
            ev_db.update_session(session_id, status="published")
            return {
                "status": "activated",
                "release_id": release_id,
                "snapshot_version": version,
                "source_commit": source_commit,
                "snapshot_trace_id": candidate.get("snapshot_trace_id"),
            }
        probe = probe_candidate(source_commit)
        gate = validate_candidate_snapshot(candidate, probe)
        already_promoted = candidate.get("status") == "production"
        previous_production = (
            candidate.get("parent_version")
            if already_promoted
            else registry_repo.get_production_version_number()
        )

        if not already_promoted:
            registry_repo.promote_candidate(
                version,
                snapshot_trace_id=gate["snapshot_trace_id"],
                runtime_identity=gate["runtime_identity"],
            )
        try:
            git_ops.commit_registry_and_push(
                f"晋升 Harness production v{version}: session={session_id}"
            )
        except Exception as promote_exc:
            registry_repo.restore_production(previous_production, version)
            compensation_error = None
            try:
                git_ops.commit_registry_and_push(
                    f"恢复 Harness production v{previous_production}: candidate v{version} registry 提交失败"
                )
            except Exception as restore_exc:
                compensation_error = str(restore_exc)
            raise RuntimeError(
                f"candidate registry 提交失败: {promote_exc}; "
                f"恢复结果: {compensation_error or 'ok'}"
            ) from promote_exc
        if release_status in {"committed", "activation_failed"}:
            append_release_event(
                release_id=release_id,
                status="registry_promoted",
                candidate_id=candidate_id,
                actor_user_id=actor_user_id,
            )
            release_status = "registry_promoted"

        try:
            activated = reload_executor(version)
            activated_identity = activated.get("runtime_identity") or {}
            if activated.get("commit") != source_commit:
                raise RuntimeError(
                    f"executor commit mismatch: {activated.get('commit')} != {source_commit}"
                )
            if (
                activated_identity.get("identity_digest")
                != gate["runtime_identity"].get("identity_digest")
            ):
                raise RuntimeError("executor runtime identity mismatch")
        except Exception as exc:
            append_release_event(
                release_id=release_id,
                status="activation_failed",
                candidate_id=candidate_id,
                actor_user_id=actor_user_id,
            )
            ev_db.update_session(session_id, status="pending_review")
            rollback_error = None
            try:
                registry_repo.restore_production(previous_production, version)
                git_ops.commit_registry_and_push(
                    f"恢复 Harness production v{previous_production}: candidate v{version} 激活失败"
                )
                restored = reload_executor(previous_production or 0)
                previous = registry_repo.get_production_version()
                if previous and restored.get("commit") != previous.get("commit_hash"):
                    raise RuntimeError("executor 未恢复到原 production commit")
            except Exception as restore_exc:
                rollback_error = str(restore_exc)
                logger.exception(
                    "candidate 激活失败后的 executor 恢复也失败: version=%s", version
                )
            raise HTTPException(
                status_code=502,
                detail={
                    "message": f"candidate v{version} 激活失败，已恢复原 production：{exc}",
                    "release_id": release_id,
                    "release_status": "activation_failed",
                    "executor_restore_error": rollback_error,
                },
            ) from exc

        if release_status == "registry_promoted":
            append_release_event(
                release_id=release_id,
                status="executor_refresh_ack",
                candidate_id=candidate_id,
                actor_user_id=actor_user_id,
            )
            release_status = "executor_refresh_ack"
        if release_status == "executor_refresh_ack":
            append_release_event(
                release_id=release_id,
                status="activated",
                candidate_id=candidate_id,
                actor_user_id=actor_user_id,
            )

        ev_db.update_session(session_id, status="published")

        logger.info(
            "进化 candidate 晋升成功: session=%s v%s commit=%s",
            session_id, version, source_commit,
        )
        return {
            "status": "activated",
            "release_id": release_id,
            "snapshot_version": version,
            "source_commit": source_commit,
            "snapshot_trace_id": gate["snapshot_trace_id"],
        }
    except HTTPException:
        raise
    except ValueError as exc:
        logger.info("candidate 发布门禁未通过: session=%s error=%s", session_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("发版失败: session=%s", session_id)
        raise HTTPException(status_code=500, detail=f"发版失败：{exc}") from exc


@router.post("/evolve/sessions/{session_id}/discard")
async def discard_session(session_id: str) -> dict[str, Any]:
    """丢弃：回退 working 区到上一 production 版本（S9）+ 清 checkpoint（Phase 3）。

    流程：
      1. 校验 session 状态为 pending_review
      2. 取当前 production 快照的 source_commit
      3. git reset --hard 回退 working 区到该 commit
      4. 推进 status → discarded（working 区解锁）
    """
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")
    if session.get("status") != "pending_review":
        raise HTTPException(
            status_code=400,
            detail=f"session 状态为 {session.get('status')}，只有 pending_review 可丢弃",
        )

    from app.core import git_ops
    from app.versioning import registry_repo

    try:
        # 取当前 production 的 commit（git log 推导）
        prod = registry_repo.get_production_version()
        if prod is None:
            raise HTTPException(
                status_code=409,
                detail="无 production 版本，无法回退（首次发版前不能丢弃）",
            )
        target_commit = registry_repo.get_version_commit(prod["version"])
        if not target_commit:
            raise HTTPException(
                status_code=409,
                detail=f"production v{prod['version']} 无对应 commit，无法回退",
            )

        # git reset --hard 回退 working 区
        import subprocess
        wd = git_ops.work_dir()
        result = subprocess.run(
            ["git", "reset", "--hard", target_commit],
            cwd=wd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git reset 失败: {result.stderr.strip()}")

        # 推进状态
        ev_db.update_session(session_id, status="discarded")

        # Phase 3：清理 checkpoint db（决策 I/T5）——discarded session 的对话状态
        # 不再需要，删文件释放空间。失败不影响主流程（最多留个孤儿文件）。
        await _cleanup_checkpoint(session_id)

        logger.info(
            "进化丢弃: session=%s reset to %s",
            session_id, target_commit,
        )
        return {
            "status": "discarded",
            "reset_to": target_commit,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("丢弃失败: session=%s", session_id)
        raise HTTPException(status_code=500, detail=f"丢弃失败：{e}")


# ── 对话式共创（Phase 3，决策 T2/T10）─────────────────────────


class EvolveMessageRequest(BaseModel):
    """用户发消息请求体。"""

    content: str  # 用户消息正文（markdown，决策 X）


@router.post("/evolve/sessions/{session_id}/messages", status_code=202)
async def send_message(session_id: str, req: EvolveMessageRequest) -> dict[str, Any]:
    """用户发消息，触发一轮对话（决策 T2 按需触发）。

    行为（决策 T2/H/J）：
      1. 校验 session 存在 + status=conversing
      2. 持久化用户消息到 evolve_messages（决策 H 完全持久化）
      3. 启动后台 task 跑 converse round（Agent 回复 + 可能调进化点工具）
      4. 立即返回 message_id（不阻塞，Agent 回复通过 SSE 推送）

    Args:
        session_id: session id
        req.content: 用户消息正文
    Returns:
        {message_id, seq, session_id, status}
    """
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")
    if session.get("status") != STATUS_CONVERSING:
        raise HTTPException(
            status_code=409,
            detail=(
                f"session 状态为 {session.get('status')}，"
                f"只有 conversing 可发消息（启动会话调 /start-converse）"
            ),
        )

    # 持久化用户消息（决策 H）
    from app.evolve.evolve_repo import EvolveMessagesRepo
    msg = EvolveMessagesRepo.append(
        session_id, role="user", content=req.content,
    )

    # 重建 ctx（按需触发模型——不持有进程内 ctx，每次从 DB 重建）
    ctx = _rebuild_ctx_from_db(session_id)
    if ctx is None:
        raise HTTPException(
            status_code=500,
            detail=f"重建 ctx 失败（session {session_id} 缺 eval_ref 或评估报告）",
        )

    # 启动 converse round（不传整条对话历史——LangGraph 通过 thread_id 从 checkpoint 取）
    from app.evolve.agent.agent import run_converse_round
    task = asyncio.create_task(_run_round_bg(ctx, run_converse_round, req.content))
    _running_tasks[session_id] = task

    logger.info("session %s: 用户消息触发 converse round (seq=%d)", session_id, msg["seq"])
    return {
        "message_id": msg["id"],
        "seq": msg["seq"],
        "session_id": session_id,
        "status": "conversing",
    }


@router.post("/evolve/sessions/{session_id}/finalize", status_code=202)
async def finalize_session(session_id: str) -> dict[str, Any]:
    """用户拍板，触发落地（决策 C/D/T10）。

    前置（决策 C/A）：
      - session.status = conversing
      - 至少 1 个 accepted 进化点

    行为：
      1. 从 accepted 进化点生成 design_doc.md（决策 T3/U）
      2. status = finalizing（FlowGuard 解锁落地工具）
      3. 后台 task 跑 finalize round（Agent 落地 → validate → change_log）
      4. 成功 → pending_review → 前端自动跳 review-report（决策 AA，前端实现）
         失败 → failed

    Returns:
        {session_id, status, accepted_count, design_doc_path}
    """
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")
    if session.get("status") != STATUS_CONVERSING:
        raise HTTPException(
            status_code=409,
            detail=(
                f"session 状态为 {session.get('status')}，"
                f"只有 conversing 可拍板（先 /start-converse + 对话）"
            ),
        )

    # 校验至少 1 个 accepted 进化点（决策 C/A）
    from app.evolve.evolve_repo import EvolvePointsRepo
    accepted_count = EvolvePointsRepo.count_accepted(session_id)
    if accepted_count == 0:
        raise HTTPException(
            status_code=400,
            detail="拍板失败：没有 accepted 进化点（至少需要 1 个，决策 A）",
        )

    # 重建 ctx
    ctx = _rebuild_ctx_from_db(session_id)
    if ctx is None:
        raise HTTPException(
            status_code=500,
            detail=f"重建 ctx 失败（session {session_id} 缺 eval_ref 或评估报告）",
        )

    # 启动 finalize round（内部会生成 design_doc + 切 finalizing + Agent 落地）
    from app.evolve.agent.agent import run_finalize_round
    task = asyncio.create_task(_run_round_bg(ctx, run_finalize_round))
    _running_tasks[session_id] = task

    logger.info(
        "session %s: 用户拍板触发 finalize round（%d 个 accepted 进化点）",
        session_id, accepted_count,
    )
    return {
        "session_id": session_id,
        "status": "finalizing",
        "accepted_count": accepted_count,
    }


def _rebuild_ctx_from_db(session_id: str) -> EvolveContext | None:
    """从 DB 重建进化上下文（决策 T2 按需触发——每次请求都重建）。

    按需触发模型下，ctx 不在进程内常驻。每条用户消息/拍板请求都重建：
      - session 元数据（status / trace_id / design_doc_path 等）
      - eval_snapshot（从 eval_ref 反查 evaluation_sessions）
      - recorder 注入

    缺 eval_ref 或评估报告缺失时返回 None（调用方报 500）。
    """
    session = ev_db.get_session(session_id)
    if session is None:
        return None

    ctx = EvolveContext(session_id=session_id)
    ctx.recorder = get_recorder()
    ctx.design_doc_path = session.get("design_doc_path") or ""
    ctx.change_log_path = session.get("change_log_path") or ""
    ctx.session_status = session.get("status") or STATUS_RUNNING
    ctx.thread_id = session_id  # thread_id 始终 = session_id（决策 T1）

    # 阶段 D：优先按 bound_eval_dossier_id 加载评估卷宗（永久绑定，不可变）
    bound_eval_dossier_id = session.get("bound_eval_dossier_id")
    if bound_eval_dossier_id:
        try:
            eval_dossier = _resolve_eval_dossier(bound_eval_dossier_id, _skip_cancel_check=True)
            ctx.eval_dossier = eval_dossier
            ctx.eval_dossier_id = bound_eval_dossier_id
            ctx.trace_id_self = session.get("self_trace_id") or ""
            ctx.trace_id = eval_dossier.get("trace_id") or ""
            ctx.origin_layer = _resolve_origin_layer(ctx.trace_id) if ctx.trace_id else None
            ctx.eval_snapshot = {
                "eval_dossier_id": bound_eval_dossier_id,
                "trace_id": ctx.trace_id,
                "scores": eval_dossier.get("scores"),
                "findings": eval_dossier.get("findings"),
                "report_md": eval_dossier.get("report_md"),
            }
            return ctx
        except HTTPException:
            # 评估卷宗丢失（级联删除等），降级到旧链路重建（向后兼容）
            logger.warning("session %s 的评估卷宗 %s 不可用，降级重建",
                           session_id, bound_eval_dossier_id)

    # 向后兼容：旧 session（无 bound_eval_dossier_id）按 eval_ref + baseline_trace 重建
    ctx.trace_id = session.get("baseline_trace") or ""
    ctx.origin_layer = _resolve_origin_layer(ctx.trace_id) if ctx.trace_id else None
    eval_ref = session.get("eval_ref")
    if eval_ref:
        ev = eval_repo.get_session(eval_ref)
        if ev:
            ctx.eval_snapshot = {
                "eval_id": ev.get("eval_id"),
                "trace_id": ev.get("trace_id"),
                "scores": ev.get("scores"),
                "findings": ev.get("findings"),
                "report_md": ev.get("report_md"),
            }
            if not ctx.trace_id:
                ctx.trace_id = ev.get("trace_id") or ""

    return ctx


async def _cleanup_checkpoint(session_id: str) -> None:
    """清理 session 的 checkpoint db（决策 I/T5）。

    discarded/failed session 不再需要对话状态，删文件释放空间。
    失败不影响主流程（最多留个孤儿文件，下次进程重启或手动清理）。
    """
    try:
        from app.evolve.agent.checkpoint_pool import get_checkpoint_pool
        await get_checkpoint_pool().drop(session_id)
    except Exception:
        logger.warning("清理 checkpoint 失败: session=%s", session_id, exc_info=True)


__all__ = ["router"]
