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
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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
    """进化启动请求（强前置：trace 必须已被评估 Agent 评估过，S8/T2）。"""

    trace_id: str  # 必填：被进化的 trace（必须已有评估报告）


class EvolveStartResponse(BaseModel):
    session_id: str
    trace_id: str
    eval_id: str  # 关联的评估 session
    status: str  # started


# ── 触发 ────────────────────────────────────────────────────


@router.post("/evolve/start", response_model=EvolveStartResponse, status_code=202)
async def evolve_start(
    req: EvolveStartRequest,
) -> EvolveStartResponse:
    """触发一次进化（方案→执行两阶段，产出待审改动，单体兼容入口）。

    强前置校验（S8）：trace 必须已有评估 Agent 产出的 done 评估报告。

    注意：Phase 3 新增对话式入口 POST /start-converse，本端点保留单体行为不变
    （决策：新老并存，零回归）。Phase 4 新前端就绪后可废弃本端点。
    """
    # 强前置校验 + working 区锁
    eval_session = _resolve_evaluated_trace(req.trace_id)
    active = _find_active_session()
    if active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"当前有未结束的进化会话（session {active['session_id']}，状态 {active['status']}），"
                f"请先发布/丢弃/取消后再启动新进化"
            ),
        )

    session_id = uuid.uuid4().hex[:12]
    ev_db.create_session(session_id, case_id="")
    ctx = _build_evolve_ctx(session_id, req.trace_id, eval_session)

    # 后台跑进化驱动器（create_task 拿到 task 引用，存注册表供 stop 端点取消）
    task = asyncio.create_task(_run_evolve_bg(ctx, req.trace_id))
    _running_tasks[session_id] = task

    logger.info(
        "进化 session 启动（单体）: session=%s trace=%s eval=%s",
        session_id, req.trace_id, eval_session["eval_id"],
    )
    return EvolveStartResponse(
        session_id=session_id, trace_id=req.trace_id,
        eval_id=eval_session["eval_id"], status="started",
    )


@router.post("/evolve/start-converse", response_model=EvolveStartResponse, status_code=202)
async def evolve_start_converse(req: EvolveStartRequest) -> EvolveStartResponse:
    """触发对话式共创进化（Phase 3，决策 T2/T10）。

    与单体 /start 的差异：内部走 inspect round（探查 + Agent 开场白），
    跑完后 status 自动转 conversing，等用户在对话区发消息（POST /messages）。

    强前置校验、working 区锁定、上下文构建与 /start 完全一致（共用 helper）。
    新前端「进化工作台」Tab 应调本端点而非 /start。
    """
    eval_session = _resolve_evaluated_trace(req.trace_id)
    active = _find_active_session()
    if active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"当前有未结束的进化会话（session {active['session_id']}，状态 {active['status']}），"
                f"请先发布/丢弃/取消后再启动新进化"
            ),
        )

    session_id = uuid.uuid4().hex[:12]
    ev_db.create_session(session_id, case_id="")
    ctx = _build_evolve_ctx(session_id, req.trace_id, eval_session)

    # 后台跑 inspect round（探查 + 开场白 → 转 conversing）
    from app.evolve.agent.agent import run_inspect_round
    task = asyncio.create_task(_run_round_bg(ctx, run_inspect_round, req.trace_id))
    _running_tasks[session_id] = task

    logger.info(
        "进化 session 启动（对话式）: session=%s trace=%s eval=%s",
        session_id, req.trace_id, eval_session["eval_id"],
    )
    return EvolveStartResponse(
        session_id=session_id, trace_id=req.trace_id,
        eval_id=eval_session["eval_id"], status="started_converse",
    )


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


def _resolve_evaluated_trace(trace_id: str) -> dict[str, Any]:
    """校验 trace 存在 + 已有 done 评估报告 + findings 结构化（决策 S8）。

    Raises:
        HTTPException: trace 不存在 / 未评估 / findings 缺失。
    Returns:
        评估 session dict（含 eval_id/findings/scores/report_md）。
    """
    from app.view.traces import get_trace
    try:
        get_trace(trace_id)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=f"trace {trace_id} 不存在",
        )

    eval_session = eval_repo.get_done_by_trace(trace_id)
    if eval_session is None:
        raise HTTPException(
            status_code=400,
            detail=f"trace {trace_id} 尚未评估，请先在评估功能中评估后再启动进化",
        )

    findings = eval_session.get("findings")
    if not findings or not isinstance(findings, list):
        raise HTTPException(
            status_code=400,
            detail=(
                f"trace {trace_id} 的评估报告缺少结构化诊断（findings 为空），"
                f"可能是评估时基础设施故障产出的降级报告。请重新评估后再启动进化"
            ),
        )
    return eval_session


def _build_evolve_ctx(session_id: str, trace_id: str, eval_session: dict[str, Any]) -> EvolveContext:
    """构建进化上下文：加载评估快照 + 注入 recorder + 关联 eval_ref。

    单体 /start 和对话式 /start-converse 共用。
    """
    ctx = EvolveContext(session_id=session_id)
    ctx.recorder = get_recorder()
    ctx.trace_id = trace_id
    ctx.origin_layer = _resolve_origin_layer(trace_id)
    ctx.eval_snapshot = {
        "eval_id": eval_session["eval_id"],
        "trace_id": eval_session.get("trace_id"),
        "scores": eval_session.get("scores"),
        "findings": eval_session.get("findings"),
        "report_md": eval_session.get("report_md"),
    }
    ev_db.update_session(session_id, eval_ref=eval_session["eval_id"])
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
    """手动停止运行中的进化 session。

    双路收敛，避免状态分裂（session 表 cancelled 但 runs 表 running）：
      1. task.cancel()：让 Agent 在下一个 await 点抛 CancelledError，
         run_evolve_session 的 except 分支会调 recorder.cancel_run 正常收尾。
      2. recorder.cancel_run(trace_id_self)：强制收敛——即便 Agent 卡在
         无 await 的底层（同步阻塞/吞 CancelledError 的循环）导致 task.cancel
         无效，也能立即把 runs.status 推进到 cancelled 并清内存活跃集合。
         幂等：trace 已被路径 1 收敛时 no-op。

    已知边界：Agent 若停在改源码中途，harnesses/current/ 下可能留脏文件，
    本端点不清理（由用户手动 stash / 重置）。
    """
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")
    # 可停止的状态：running / conversing / finalizing（pending_review 走 publish/discard）。
    # Phase 1（状态机骨架）：统一标 cancelled，与原 running 单体行为一致。
    # Phase 3（对话式 API 改造）会按决策 L 细化——conversing 的 stop 只取消当前输出
    # task、不推进 status，会话保留可继续输入。
    stoppable = {STATUS_RUNNING, STATUS_CONVERSING, STATUS_FINALIZING}
    if session.get("status") not in stoppable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"session 状态为 {session.get('status')}，"
                f"只有 running/conversing/finalizing 可停止"
            ),
        )

    task = _running_tasks.get(session_id)
    if task is not None and not task.done():
        task.cancel()
        logger.info("进化 session %s 已请求取消（task.cancel）", session_id)
    else:
        # 竞态：task 已结束或不在注册表（进程重启后）。仍标 cancelled 对齐状态。
        logger.warning(
            "进化 session %s 未找到活跃 task，仅标记 cancelled", session_id
        )

    # 强制收敛 recorder trace 状态：即便 task.cancel 无效，runs.status 也立即收敛。
    # 必须在 task.cancel 之后调——若 Agent 真在 await 点退出，run_evolve_session 的
    # except 分支会再次调 cancel_run，幂等保护兜底。
    recorder = get_recorder()
    if recorder is not None:
        trace_id_self = recorder.get_trace_id_by_session(session_id)
        if trace_id_self:
            recorder.cancel_run(trace_id_self, reason="user_stop")
            logger.info("进化 session %s trace %s 已强制收敛 cancelled", session_id, trace_id_self)

    ev_db.update_session(session_id, status="cancelled")
    return {"status": "cancelled", "session_id": session_id}


# ── SSE 实时流 ──────────────────────────────────────────────


@router.get("/evolve/sessions/{session_id}/stream")
async def stream_session(session_id: str) -> StreamingResponse:
    """SSE 实时推送进化 Agent 的执行步骤（D3/D4：从 recorder trace 事件流派生）。"""
    recorder = get_recorder()
    if recorder is None:
        raise HTTPException(status_code=503, detail="trace recorder 未启动")

    # 按 session_id 查自观测 trace_id。
    trace_id_self = recorder.get_trace_id_by_session(session_id)
    if trace_id_self is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 事件流不存在")

    queue = recorder.get_active_queue(trace_id_self)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'start', 'session_id': session_id}, ensure_ascii=False)}\n\n"

            while True:
                if queue is None:
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

                # trace 事件 → SSE 帧派生。
                frame = _trace_event_to_sse(event)
                if frame:
                    yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"

                if event.type in ("run_end", "run_error", "run_cancelled"):
                    end_type = "end" if event.type == "run_end" else "error"
                    yield f"data: {json.dumps({'type': end_type}, ensure_ascii=False)}\n\n"
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
            "X-Accel-Buffering": "no",
        },
    )


def _trace_event_to_sse(event: Any) -> dict[str, Any] | None:
    """trace 事件 → 前端 SSE 帧派生（与 eval_agent/api.py 对称）。

    Phase 5 事件协议扩展：识别 emit_step 的特殊 tool 名，派生对应 SSE 帧类型：
      - tool="phase"            → {type:"phase", phase}
      - tool="proposal"         → {type:"proposal", action, point_id, seq, target, ...}
      - tool="finalizing"       → {type:"finalizing", event, target, ...}
      - 含 message 字段（无 tool） → {type:"log", message}
      - 含 tool 字段（其他）       → {type:"step", **data}（保留原行为）
    """
    if event.type != "run_meta" or not event.input:
        return None
    data = event.input if isinstance(event.input, dict) else {}
    tool = data.get("tool", "")

    # Phase 5：阶段切换事件
    if tool == "phase":
        phase = data.get("phase")
        if phase:
            return {"type": "phase", "phase": phase}
        return None

    # Phase 5：进化点状态变更（决策 B/M 浮窗实时同步）
    if tool == "proposal":
        return {
            "type": "proposal",
            "action": data.get("action"),
            "point_id": data.get("point_id"),
            "seq": data.get("seq"),
            "target": data.get("target"),
            "chosen_option": data.get("chosen_option"),
        }

    # Phase 5：落地进度事件（决策 W）
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
def publish_session(session_id: str) -> dict[str, Any]:
    """发版：把进化改动固化为新 Agent 版本（去 DB 重构）。

    流程（4步）：
      1. 更新 registry.json（publish_version：append 新版本 + 移 production 指针）
      2. git commit + push（registry 变更 + 源码改动在同一个 commit，单 commit 原子性）
      3. 通知执行端（/reload）
      4. 推进 session status → published
    """
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
    from app.versioning.snapshot_publisher import notify_executor

    try:
        # 1. 更新 registry.json（源码改动已在 repo/ 工作目录，evolve 落盘的）
        entry = registry_repo.publish_version(
            change_summary=f"进化 session {session_id} 产出的改动",
            source_session=session_id,
        )

        # 2. git commit + push（registry 变更 + 源码改动 → 同一个 commit，原子）
        commit_msg = f"进化发版 v{entry['version']}: session={session_id}"
        source_commit = git_ops.commit_and_push(commit_msg)

        # 3. 通知执行端
        notified = notify_executor(entry["version"])

        # 4. 推进状态
        ev_db.update_session(session_id, status="published")

        logger.info(
            "进化发版成功: session=%s v%s commit=%s",
            session_id, entry["version"], source_commit,
        )
        return {
            "status": "published",
            "snapshot_version": entry["version"],
            "source_commit": source_commit,
            "notified": notified,
        }
    except Exception as e:
        logger.exception("发版失败: session=%s", session_id)
        raise HTTPException(status_code=500, detail=f"发版失败：{e}")


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
    ctx.trace_id = session.get("baseline_trace") or ""
    ctx.design_doc_path = session.get("design_doc_path") or ""
    ctx.change_log_path = session.get("change_log_path") or ""
    ctx.session_status = session.get("status") or STATUS_RUNNING
    ctx.thread_id = session_id  # thread_id 始终 = session_id（决策 T1）
    ctx.origin_layer = _resolve_origin_layer(ctx.trace_id) if ctx.trace_id else None

    # 从 eval_ref 反查评估快照
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
