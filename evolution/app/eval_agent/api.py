"""eval_agent API —— 评估 Agent 触发 + 查询 + SSE（决策 S7 + D3/D4）。

端点（/api/eval-agent 前缀，避免与 view/evaluation_api 的 /api/evaluation 撞路径）：
  POST /api/eval-agent/start           启动评估（传 trace_id，异步，返回 eval_id）
  GET  /api/eval-agent/sessions        评估 session 列表（最新在前，支持 ?trace_id= 过滤）
  GET  /api/eval-agent/sessions/{id}   单评估详情（含 scores/findings/report_md）
  GET  /api/eval-agent/sessions/{id}/stream   SSE 实时事件流
  GET  /api/eval-agent/evaluated-traces  已评估的 trace 列表（进化入口「选已评估trace」用）

执行模型（D3/D4：trace 统一接管 SSE）：
  start 时注入 recorder 到 ctx → 后台 task 跑评估 Agent → recorder 产 trace 事件 →
  SSE 从 recorder 队列消费推前端。SessionEvents 已删除。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import app.core.db as db
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.eval_agent import repo as eval_repo
from app.eval_agent.agent import run_eval_session
from app.eval_agent.ctx import EvaluationContext
from app.trace.recorder import EvolutionTraceRecorder

logger = logging.getLogger("evolution.eval_agent.api")

router = APIRouter(prefix="/eval-agent", tags=["eval-agent"])


def get_recorder() -> EvolutionTraceRecorder | None:
    """获取全局 recorder 实例（main.py lifespan 注入到 app.state）。

    D5：recorder 在 lifespan 显式创建。这里从 app.state 取。
    未启动时返回 None（兼容早期未挂载场景）。
    """
    from app.main import app
    return getattr(app.state, "trace_recorder", None)


class EvalStartRequest(BaseModel):
    """评估启动请求。"""

    trace_id: str  # 必填：要评估的 trace


class EvalStartResponse(BaseModel):
    eval_id: str
    trace_id: str
    status: str  # started


# ── 触发 ────────────────────────────────────────────────────


@router.post("/start", response_model=EvalStartResponse, status_code=202)
async def eval_start(
    req: EvalStartRequest,
    background_tasks: BackgroundTasks,
) -> EvalStartResponse:
    """启动一次评估（异步）。立即返回 eval_id，后台跑评估 Agent。"""
    # 校验 trace 存在
    from app.view.traces import get_trace
    try:
        get_trace(req.trace_id)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=f"trace {req.trace_id} 不存在",
        )

    eval_id = uuid.uuid4().hex[:12]

    # 从 manual_tests 反查该 trace 对应的 Agent 版本（T7）
    version_type, version_id = _lookup_agent_version(req.trace_id)

    # 落库评估 session
    eval_repo.create_session(
        eval_id, req.trace_id,
        agent_version_type=version_type,
        agent_version_id=version_id,
    )

    # 构建评估上下文 + 注入 recorder（D6）
    ctx = EvaluationContext(
        eval_id, req.trace_id,
        agent_version_type=version_type,
        agent_version_id=version_id,
    )
    ctx.recorder = get_recorder()

    # 后台跑评估 Agent
    background_tasks.add_task(_run_eval_bg, ctx)

    logger.info(
        "评估 session 启动: eval=%s trace=%s version=%s/%s",
        eval_id, req.trace_id, version_type, version_id,
    )
    return EvalStartResponse(eval_id=eval_id, trace_id=req.trace_id, status="started")


def _lookup_agent_version(trace_id: str) -> tuple[str | None, int | None]:
    """从 manual_tests 表反查 trace 对应的 Agent 版本（T7）。

    一条 trace 可能被多次测试关联，取最新的一条 done 测试记录。
    """
    row = db.query_one(
        """SELECT version_type, version_id FROM manual_tests
           WHERE trace_id = ? AND status = 'done'
           ORDER BY created_at DESC LIMIT 1""",
        (trace_id,),
    )
    if row:
        return row["version_type"], row["version_id"]
    return None, None


async def _run_eval_bg(ctx: EvaluationContext) -> None:
    """后台执行评估 Agent。

    D3/D4：trace 终态（complete/fail_run）已在 run_eval_session 内处理，
    这里只管 evaluation_sessions 表的业务状态。
    """
    try:
        result = await run_eval_session(ctx)
        if result["status"] == "done":
            # Agent 正常结束。但「正常结束」≠「产出了报告」：
            # write_eval_report 工具若被调用，DB 状态已是 done 且写了报告。
            # 若 Agent 没调报告工具就结束，DB 仍是 running —— 此时报告缺失，
            # 应视为失败，避免 session 永远停在 running。
            session = eval_repo.get_session(ctx.eval_id)
            if session is None or session.get("status") != "done":
                eval_repo.update_session(ctx.eval_id, status="failed")
        else:
            eval_repo.update_session(ctx.eval_id, status="failed")
    except Exception as e:
        logger.exception("评估 session %s 后台执行异常", ctx.eval_id)
        eval_repo.update_session(ctx.eval_id, status="failed")


# ── 查询 ────────────────────────────────────────────────────


@router.get("/sessions")
def list_sessions(
    trace_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """列出评估 session（最新在前）。可按 trace_id 过滤。"""
    sessions = eval_repo.list_sessions(trace_id=trace_id, limit=limit)
    return {"sessions": sessions, "total": len(sessions)}


@router.get("/sessions/{eval_id}")
def get_session(eval_id: str) -> dict[str, Any]:
    """查单个评估 session 详情（含 scores/findings/report_md）。"""
    session = eval_repo.get_session(eval_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"评估 session {eval_id} 不存在")
    return session


@router.get("/evaluated-traces")
def list_evaluated_traces(limit: int = 100) -> dict[str, Any]:
    """列已评估（有 done 记录）的 trace（进化入口「选已评估 trace」用）。"""
    traces = eval_repo.list_evaluated_traces(limit=limit)
    return {"traces": traces, "total": len(traces)}


# ── SSE 实时流 ──────────────────────────────────────────────


@router.get("/sessions/{eval_id}/stream")
async def stream_session(eval_id: str) -> StreamingResponse:
    """SSE 实时推送评估 Agent 的执行步骤（D3/D4：从 recorder trace 事件流派生）。

    recorder 的 trace 事件（business_step + llm/tool 框架事件）派生为前端可消费的
    step/log 帧。trace 终态时推送 end/error 帧并关闭流。
    """
    recorder = get_recorder()
    if recorder is None:
        raise HTTPException(status_code=503, detail="trace recorder 未启动")

    # 按 eval_id（session_id）查自观测 trace_id。
    trace_id_self = recorder.get_trace_id_by_session(eval_id)
    if trace_id_self is None:
        raise HTTPException(status_code=404, detail=f"评估 session {eval_id} 事件流不存在")

    queue = recorder.get_active_queue(trace_id_self)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'start', 'eval_id': eval_id}, ensure_ascii=False)}\n\n"

            while True:
                if queue is None:
                    # trace 已终态，队列已清。推 end 帧结束。
                    yield f"data: {json.dumps({'type': 'end'}, ensure_ascii=False)}\n\n"
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if recorder.is_terminal(trace_id_self):
                        yield f"data: {json.dumps({'type': 'end'}, ensure_ascii=False)}\n\n"
                        break
                    yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
                    continue

                # trace 事件 → SSE 帧派生（D3.4）。
                frame = _trace_event_to_sse(event)
                if frame:
                    yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"

                # 终态事件 → 推 end 帧并退出。
                if event.type in ("run_end", "run_error", "run_cancelled"):
                    end_type = "end" if event.type == "run_end" else "error"
                    yield f"data: {json.dumps({'type': end_type}, ensure_ascii=False)}\n\n"
                    break
        except asyncio.CancelledError:
            logger.info("评估 session %s SSE 流被取消", eval_id)
        except Exception:
            logger.exception("评估 session %s SSE 流异常", eval_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _trace_event_to_sse(event: Any) -> dict[str, Any] | None:
    """trace 事件 → 前端 SSE 帧派生（D3.4）。

    - business_step（run_meta 含 tool/status/phase）→ step 帧
    - run_meta 含 message → log 帧
    - llm/tool 框架事件 → 可选细粒度帧（暂不推，避免刷屏）
    - 终态事件由调用方处理
    """
    if event.type == "run_meta" and event.input:
        data = event.input if isinstance(event.input, dict) else {}
        if "message" in data and "tool" not in data:
            # 纯 log 事件（emit_log 产的）
            return {"type": "log", "message": data["message"]}
        if "tool" in data:
            # step 事件（emit_step 产的）
            return {"type": "step", **data}
    return None


__all__ = ["router"]
