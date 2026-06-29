"""evolve API —— 触发 + 查询 + SSE（前端运行信息页后端）。

端点：
  POST /api/evolve/start          触发一次进化（异步，返回 session_id）
  GET  /api/evolve/sessions       session 列表（最新在前）
  GET  /api/evolve/sessions/{id}  单 session 详情（含 report）
  GET  /api/evolve/sessions/{id}/stream   SSE 实时事件流
  GET  /api/evolve/cases          评估集 case 列表

执行模型：
  start 时注册事件总线 → 后台 task 跑进化 Agent（astream 工具调用）→
  每步 emit 到队列 → SSE 端点消费推前端。
  进程重启则进行中 session 丢失（单轮手动 R8 已接受）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.evolve import db as ev_db
from app.evolve import events as ev_events
from app.evolve import evalset
from app.evolve.agent import run_evolve_pipeline, run_evolve_session
from app.evolve.tools import EvolveContext

logger = logging.getLogger("evolution.evolve.api")

router = APIRouter(tags=["evolve"])


class EvolveStartRequest(BaseModel):
    """进化启动请求（一把手模式，向后兼容）。"""

    case: str = "case-001"  # 评估集 case 标识


class EvolveStartResponse(BaseModel):
    session_id: str
    case: str
    status: str  # started


class PipelineStartRequest(BaseModel):
    """驱动器模式启动请求（6 阶段流水线，D4）。

    baseline_trace 是输入（历史 trace 池），流水线不跑 baseline 生成。
    """

    case: str = "case-001"
    baseline_trace: str  # 必填：历史 trace 池的 trace_id


class PipelineStartResponse(BaseModel):
    session_id: str
    case: str
    baseline_trace: str
    status: str  # started


# ── 触发 ────────────────────────────────────────────────────


@router.post("/evolve/start", response_model=EvolveStartResponse, status_code=202)
async def evolve_start(
    req: EvolveStartRequest,
    background_tasks: BackgroundTasks,
) -> EvolveStartResponse:
    """手动触发一次进化（单轮，R8）。

    异步启动进化 Agent，立即返回 session_id。执行走后台 task + 事件总线。
    """
    # 校验 case 存在
    if not evalset.case_exists(req.case):
        raise HTTPException(
            status_code=404,
            detail=f"评估集 case {req.case} 不存在，可用: {evalset.list_cases()}",
        )

    session_id = uuid.uuid4().hex[:12]

    # 注册事件总线 + 落库
    events = await ev_events.register(session_id)
    ev_db.create_session(session_id, req.case)

    # 构建上下文
    ctx = EvolveContext(session_id=session_id, case_id=req.case)
    ctx.events = events

    user_input = (
        f"请对评估集 case「{req.case}」执行一次完整的进化流程。"
        "按 system prompt 的步骤，从 run_baseline 开始，分析改进点，"
        "产出改动，重跑验证，最后产出报告。"
    )

    # 后台跑进化 Agent
    background_tasks.add_task(_run_evolve_bg, ctx, user_input)

    logger.info("进化 session 启动: session=%s case=%s", session_id, req.case)
    return EvolveStartResponse(session_id=session_id, case=req.case, status="started")


async def _run_evolve_bg(ctx: EvolveContext, user_input: str) -> None:
    """后台执行进化 Agent（一把手模式，后台 task）。"""
    try:
        result = await run_evolve_session(ctx, user_input)
        if ctx.events:
            if result["status"] == "done":
                ctx.events.finish("done")
            else:
                ctx.events.finish("failed", result.get("error", "未产出报告"))
    except Exception as e:
        logger.exception("进化 session %s 后台执行异常", ctx.session_id)
        if ctx.events:
            ctx.events.finish("failed", str(e))
        ev_db.update_session(ctx.session_id, status="failed")


# ── 驱动器模式触发（D4：6 阶段流水线）────────────────────────────


@router.post("/evolve/pipeline/start", response_model=PipelineStartResponse, status_code=202)
async def pipeline_start(
    req: PipelineStartRequest,
    background_tasks: BackgroundTasks,
) -> PipelineStartResponse:
    """触发驱动器模式进化流水线（6 阶段：评估baseline→方案→执行→跑candidate→评估candidate→报告）。

    baseline_trace 是输入（历史 trace 池已有），流水线不跑 baseline 生成。
    """
    if not evalset.case_exists(req.case):
        raise HTTPException(
            status_code=404,
            detail=f"评估集 case {req.case} 不存在，可用: {evalset.list_cases()}",
        )
    # 校验 baseline_trace 存在
    from app.view.traces import get_trace
    try:
        get_trace(req.baseline_trace)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=f"baseline_trace {req.baseline_trace} 不存在",
        )

    session_id = uuid.uuid4().hex[:12]
    events = await ev_events.register(session_id)
    ev_db.create_session(session_id, req.case)

    ctx = EvolveContext(session_id=session_id, case_id=req.case)
    ctx.events = events

    background_tasks.add_task(_run_pipeline_bg, ctx, req.baseline_trace)

    logger.info(
        "驱动器流水线启动: session=%s case=%s baseline=%s",
        session_id, req.case, req.baseline_trace,
    )
    return PipelineStartResponse(
        session_id=session_id, case=req.case,
        baseline_trace=req.baseline_trace, status="started",
    )


async def _run_pipeline_bg(ctx: EvolveContext, baseline_trace: str) -> None:
    """后台执行驱动器流水线（6 阶段）。"""
    try:
        result = await run_evolve_pipeline(ctx, baseline_trace)
        if ctx.events:
            if result["status"] == "done":
                ctx.events.finish("done")
            else:
                ctx.events.finish("failed", result.get("error", "未产出报告"))
    except Exception as e:
        logger.exception("驱动器流水线 %s 后台执行异常", ctx.session_id)
        if ctx.events:
            ctx.events.finish("failed", str(e))
        ev_db.update_session(ctx.session_id, status="failed")


# ── 查询 ────────────────────────────────────────────────────


@router.get("/evolve/sessions")
def list_sessions(limit: int = 50) -> dict[str, Any]:
    """列出进化 session（最新在前）。"""
    sessions = ev_db.list_sessions(limit=limit)
    return {"sessions": sessions, "total": len(sessions)}


@router.get("/evolve/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    """查单个 session 详情。"""
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")
    return session


# ── SSE 实时流 ──────────────────────────────────────────────


@router.get("/evolve/sessions/{session_id}/stream")
async def stream_session(session_id: str) -> StreamingResponse:
    """SSE 实时推送进化 Agent 的执行步骤。

    事件类型：step / log / report / error / end
    """
    events = ev_events.get(session_id)
    if events is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 事件流不存在")

    async def event_generator():
        try:
            # 先补一个 session 开始事件
            yield f"data: {json.dumps({'type': 'start', 'session_id': session_id}, ensure_ascii=False)}\n\n"

            while True:
                # 非阻塞取事件
                try:
                    event = await asyncio.wait_for(events.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # 超时：检查是否已终结
                    if events.terminal is not None:
                        break
                    # 发心跳保活
                    yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
                    continue

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

                # 终结事件后退出
                if event.get("type") in ("end", "error"):
                    break
        except asyncio.CancelledError:
            logger.info("session %s SSE 流被取消", session_id)
        except Exception:
            logger.exception("session %s SSE 流异常", session_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 不缓冲
        },
    )


# ── 评估集 ──────────────────────────────────────────────────


@router.get("/evolve/cases")
def list_cases() -> dict[str, Any]:
    """列出评估集所有 case（带 title）。"""
    return {"cases": evalset.list_cases_with_title()}


@router.get("/evolve/cases/{case_id}")
def get_case(case_id: str) -> dict[str, Any]:
    """取单个 case 的 demand.md 全文 + title。"""
    if not evalset.case_exists(case_id):
        raise HTTPException(status_code=404, detail=f"case not found: {case_id}")
    _, title, demand_md = evalset.load_case(case_id)
    return {"case_id": case_id, "title": title, "demand_md": demand_md}


__all__ = ["router"]
