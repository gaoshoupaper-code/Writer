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
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.eval_agent import repo as eval_repo
from app.eval_agent.agent import run_eval_session
from app.eval_agent.ctx import EvaluationContext
from app.trace.recorder import EvolutionTraceRecorder

logger = logging.getLogger("evolution.eval_agent.api")

router = APIRouter(prefix="/eval-agent", tags=["eval-agent"])

# eval_id → 后台评估 task。stop 端点靠它 cancel 正在跑的 Agent。
# 原先用 FastAPI BackgroundTasks.add_task 不持有 task 引用，外部无法取消；
# 改用 asyncio.create_task 后存这里，stop 才能调 task.cancel()。
_running_tasks: dict[str, asyncio.Task] = {}


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

    # 防自观测：评估 Agent 只评估创作 Agent 的 trace，不能评估进化端自观测 trace
    # （evolution_eval = 评估 Agent 自身录像，evolution_evolve = 进化 Agent 录像）。
    # 否则会形成"评估自己"的死循环或跨 Agent 误评。
    run_row = db.query_one(
        "SELECT run_purpose FROM runs WHERE trace_id = ?", (req.trace_id,)
    )
    run_purpose = (run_row or {}).get("run_purpose") or "user_generation"
    if run_purpose in ("evolution_eval", "evolution_evolve"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"trace {req.trace_id} 是进化端自观测 trace"
                f"（run_purpose={run_purpose}），不能评估。"
                "只能评估创作 Agent 的 trace。"
            ),
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

    # 后台跑评估 Agent（create_task 拿到 task 引用，存注册表供 stop 端点取消）
    task = asyncio.create_task(_run_eval_bg(ctx))
    _running_tasks[eval_id] = task

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
        elif result["status"] == "cancelled":
            # 用户主动停止的合法终态，不算失败。
            pass
        else:
            eval_repo.update_session(ctx.eval_id, status="failed")
    except asyncio.CancelledError:
        # 取消在进 session 函数前命中（兜底）。run_eval_session 内部已处理时不会走到这。
        logger.info("评估 session %s 在后台被取消", ctx.eval_id)
        eval_repo.update_session(ctx.eval_id, status="cancelled")
        raise
    except Exception as e:
        logger.exception("评估 session %s 后台执行异常", ctx.eval_id)
        eval_repo.update_session(ctx.eval_id, status="failed")
    finally:
        _running_tasks.pop(ctx.eval_id, None)


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


# ── trace 稳定性重构：Pull 模式事件流（替代 SSE，设计 20260720_203000）──


class EvalEventsSinceResponse(BaseModel):
    """评估 session 事件游标拉取响应（替代 SSE /stream）。"""
    frames: list[dict[str, Any]]   # 从 run_meta 派生的 step/log 帧（按 sequence 升序）
    max_seq: int                    # 本次返回的最大 sequence（前端下次 since_seq）；无事件时 = since_seq
    has_more: bool                  # 是否还有更多事件未拉
    eval_status: str                # 评估 session 当前状态（running/done/failed），前端据此判断是否继续轮询


@router.get("/sessions/{eval_id}/events/since", response_model=EvalEventsSinceResponse)
def get_session_events_since(
    eval_id: str,
    since_seq: int = Query(0, ge=0, description="返回 sequence > since_seq 的事件"),
    limit: int = Query(500, ge=1, le=1000, description="单次返回上限"),
) -> EvalEventsSinceResponse:
    """按 sequence 游标拉取评估 session 的事件帧（trace 稳定性重构，Pull 主导）。

    替代 GET /sessions/{id}/stream SSE：前端轮询本接口拿 step/log 帧，
    断了下个 tick 自动恢复。语义对齐 Phase 2 的 /traces/{id}/events/since。

    实现：从 evaluation_sessions.self_trace_id 反查 trace_id → 查 event_payloads
    表的 run_meta 事件 → 用 _trace_event_to_sse 派生成 step/log 帧。
    """
    session = eval_repo.get_session(eval_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"评估 session {eval_id} 不存在")

    self_trace_id = session.get("self_trace_id")
    frames: list[dict[str, Any]] = []
    max_seq = since_seq

    if self_trace_id:
        # 拉增量事件（limit+1 探测 has_more）。
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
                    # 附带 sequence 让前端能去重（轮询重试时不会重复渲染）。
                    frame["_seq"] = seq
                    frames.append(frame)
            except Exception:
                # 单条解析失败不阻断其它事件。
                continue
    else:
        # self_trace_id 为 None：评估是 Phase 0 之前的旧 session，无录像。
        # 不算错——直接返回空帧，eval_status 让前端知道是否继续轮询。
        has_more = False

    return EvalEventsSinceResponse(
        frames=frames,
        max_seq=max_seq,
        has_more=has_more,
        eval_status=session.get("status", "running"),
    )


@router.get("/evaluated-traces")
def list_evaluated_traces(limit: int = 100) -> dict[str, Any]:
    """列已评估（有 done 记录）的 trace（进化入口「选已评估 trace」用）。"""
    traces = eval_repo.list_evaluated_traces(limit=limit)
    return {"traces": traces, "total": len(traces)}


# ── 停止 ────────────────────────────────────────────────────


@router.post("/sessions/{eval_id}/stop")
def stop_session(eval_id: str) -> dict[str, Any]:
    """手动停止运行中的评估 session。

    双路收敛，避免状态分裂（session 表 cancelled 但 runs 表 running）：
      1. task.cancel()：让 Agent 在下一个 await 点抛 CancelledError，
         run_eval_session 的 except 分支会调 recorder.cancel_run 正常收尾。
      2. recorder.cancel_run(trace_id_self)：强制收敛——即便 Agent 卡在
         无 await 的底层（同步阻塞/吞 CancelledError 的循环）导致 task.cancel
         无效，也能立即把 runs.status 推进到 cancelled 并清内存活跃集合。
         幂等：trace 已被路径 1 收敛时 no-op。
    """
    session = eval_repo.get_session(eval_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"评估 session {eval_id} 不存在")
    if session.get("status") != "running":
        raise HTTPException(
            status_code=400,
            detail=f"评估 session 状态为 {session.get('status')}，只有 running 可停止",
        )

    task = _running_tasks.get(eval_id)
    if task is not None and not task.done():
        task.cancel()
        logger.info("评估 session %s 已请求取消（task.cancel）", eval_id)
    else:
        logger.warning(
            "评估 session %s 未找到活跃 task，仅标记 cancelled", eval_id
        )

    # 强制收敛 recorder trace 状态：即便 task.cancel 无效，runs.status 也立即收敛。
    # 必须在 task.cancel 之后调——若 Agent 真在 await 点退出，run_eval_session 的
    # except 分支会再次调 cancel_run，幂等保护兜底。
    recorder = get_recorder()
    if recorder is not None:
        trace_id_self = recorder.get_trace_id_by_session(eval_id)
        if trace_id_self:
            recorder.cancel_run(trace_id_self, reason="user_stop")
            logger.info("评估 session %s trace %s 已强制收敛 cancelled", eval_id, trace_id_self)

    eval_repo.update_session(eval_id, status="cancelled")
    return {"status": "cancelled", "eval_id": eval_id}


# ── SSE 实时流 ──────────────────────────────────────────────


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
