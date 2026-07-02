"""evolve API —— 进化触发 + 查询 + SSE + 发版/丢弃（三功能解耦，决策 S8/S9）。

端点：
  POST /api/evolve/start                        触发进化（强前置：trace 必须已评估，S8）
  GET  /api/evolve/sessions                     session 列表（最新在前）
  GET  /api/evolve/sessions/{id}                单 session 详情
  GET  /api/evolve/sessions/{id}/stream         SSE 实时事件流
  POST /api/evolve/sessions/{id}/publish        发版（S9/S12：git commit + bootstrap config + snapshot）
  POST /api/evolve/sessions/{id}/discard        丢弃（S9：git reset 回 production + 状态推进）

执行模型（沿用）：
  start 时注册事件总线 → 后台 task 跑进化驱动器 → emit 到队列 → SSE 消费推前端。
  进程重启则进行中 session 丢失（单轮手动，已接受）。
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

from app.eval_agent import repo as eval_repo
from app.evolve import db as ev_db
from app.common import events as ev_events
from app.evolve.driver.agent import run_evolve_session
from app.evolve.ctx import EvolveContext

logger = logging.getLogger("evolution.evolve.api")

router = APIRouter(tags=["evolve"])


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
    background_tasks: BackgroundTasks,
) -> EvolveStartResponse:
    """触发一次进化（方案→执行两阶段，产出待审改动）。

    强前置校验（S8）：trace 必须已有评估 Agent 产出的 done 评估报告。
    """
    # 校验 trace 存在
    from app.view.traces import get_trace
    try:
        get_trace(req.trace_id)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=f"trace {req.trace_id} 不存在",
        )

    # 强前置校验（S8）：trace 必须已评估
    eval_session = eval_repo.get_done_by_trace(req.trace_id)
    if eval_session is None:
        raise HTTPException(
            status_code=400,
            detail=f"trace {req.trace_id} 尚未评估，请先在评估功能中评估后再启动进化",
        )

    # working 区锁定校验（S6/4.3）：存在 pending_review 的 session 时禁止开新进化
    pending = _find_pending_review_session()
    if pending:
        raise HTTPException(
            status_code=409,
            detail=(
                f"当前有待审改动未处理（session {pending}），"
                f"请先发版或丢弃后再启动新进化"
            ),
        )

    session_id = uuid.uuid4().hex[:12]

    # 注册事件总线 + 落库
    events = await ev_events.register(session_id)
    ev_db.create_session(session_id, case_id="")

    # 构建上下文：加载评估报告快照到 ctx.eval_snapshot（S2 DB 交接）
    ctx = EvolveContext(session_id=session_id)
    ctx.events = events
    ctx.trace_id = req.trace_id
    ctx.eval_snapshot = {
        "eval_id": eval_session["eval_id"],
        "trace_id": eval_session.get("trace_id"),
        "scores": eval_session.get("scores"),
        "findings": eval_session.get("findings"),
        "report_md": eval_session.get("report_md"),
    }

    # 关联评估报告（eval_ref）
    ev_db.update_session(session_id, eval_ref=eval_session["eval_id"])

    # 后台跑进化驱动器
    background_tasks.add_task(_run_evolve_bg, ctx, req.trace_id)

    logger.info(
        "进化 session 启动: session=%s trace=%s eval=%s",
        session_id, req.trace_id, eval_session["eval_id"],
    )
    return EvolveStartResponse(
        session_id=session_id, trace_id=req.trace_id,
        eval_id=eval_session["eval_id"], status="started",
    )


def _find_pending_review_session() -> str | None:
    """查是否有 pending_review 状态的进化 session（working 区锁定，S6）。"""
    sessions = ev_db.list_sessions(limit=50)
    for s in sessions:
        if isinstance(s, dict) and s.get("status") == "pending_review":
            return s.get("session_id")
    return None


async def _run_evolve_bg(ctx: EvolveContext, trace_id: str) -> None:
    """后台执行进化驱动器（方案→执行两阶段）。"""
    try:
        result = await run_evolve_session(ctx, trace_id)
        if ctx.events:
            if result["status"] == "done":
                ctx.events.finish("done")
            else:
                ctx.events.finish("failed", result.get("error", "进化未产出 change_log"))
    except Exception as e:
        logger.exception("进化 session %s 后台执行异常", ctx.session_id)
        ev_db.update_session(ctx.session_id, status="failed")
        if ctx.events:
            ctx.events.finish("failed", str(e))


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
            yield f"data: {json.dumps({'type': 'start', 'session_id': session_id}, ensure_ascii=False)}\n\n"

            while True:
                try:
                    event = await asyncio.wait_for(events.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if events.terminal is not None:
                        break
                    yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
                    continue

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

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
            "X-Accel-Buffering": "no",
        },
    )


# ── 发版 / 丢弃（Phase 4，S9/S12）────────────────────────────


@router.post("/evolve/sessions/{session_id}/publish")
def publish_session(session_id: str) -> dict[str, Any]:
    """发版：把进化改动固化为新 Agent 版本（S9/S12）。

    流程：
      1. 校验 session 状态为 pending_review
      2. git commit + push（产 source_commit）
      3. bootstrap config（从改动后源码生成 config_json）
      4. publish_config（存 harness_snapshots 新 production + 旧 production 降 retired）
      5. 通知执行端
      6. 推进 status → published（working 区解锁）
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
    from app.harness_config.bootstrap import build_v1_config
    from app.versioning import snapshot_repo
    from app.versioning.snapshot_publisher import notify_executor

    try:
        # 1. git commit + push → source_commit
        commit_msg = f"进化发版: session={session_id} trace={session.get('baseline_trace', '')}"
        source_commit = git_ops.commit_and_push(commit_msg)

        # 2. bootstrap config
        config = build_v1_config()

        # 3. publish_config（存快照 + 旧 production 降 retired）
        snapshot = snapshot_repo.publish_config(
            config,
            source_commit=source_commit,
            change_summary=f"进化 session {session_id} 产出的改动",
        )

        # 4. 通知执行端
        notified = notify_executor(snapshot["version"])

        # 5. 推进状态
        ev_db.update_session(session_id, status="published")

        logger.info(
            "进化发版成功: session=%s snapshot_v=%s commit=%s",
            session_id, snapshot["version"], source_commit,
        )
        return {
            "status": "published",
            "snapshot_version": snapshot["version"],
            "source_commit": source_commit,
            "notified": notified,
        }
    except Exception as e:
        logger.exception("发版失败: session=%s", session_id)
        raise HTTPException(status_code=500, detail=f"发版失败：{e}")


@router.post("/evolve/sessions/{session_id}/discard")
def discard_session(session_id: str) -> dict[str, Any]:
    """丢弃：回退 working 区到上一 production 版本（S9）。

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
    from app.versioning import snapshot_repo

    try:
        # 取当前 production 的 source_commit
        prod = snapshot_repo.get_production_snapshot()
        if prod is None or not prod.get("source_commit"):
            raise HTTPException(
                status_code=409,
                detail="无 production 快照或无 source_commit，无法回退（首次发版前不能丢弃）",
            )
        target_commit = prod["source_commit"]

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


__all__ = ["router"]
