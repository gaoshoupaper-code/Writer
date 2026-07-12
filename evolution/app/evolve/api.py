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
from app.evolve.driver.agent import run_evolve_session
from app.evolve.ctx import EvolveContext
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

    # 强前置校验（S8+）：评估报告必须有结构化 findings，否则 plan 端 evidence_ref 校验
    # 会因"合法 id：（无）"变成不可能完成的约束（评估基础设施故障时产出的降级报告
    # status=done 但 findings=NULL）。此时拒绝启动，提示重新评估。
    findings = eval_session.get("findings")
    if not findings or not isinstance(findings, list):
        raise HTTPException(
            status_code=400,
            detail=(
                f"trace {req.trace_id} 的评估报告缺少结构化诊断（findings 为空），"
                f"可能是评估时基础设施故障产出的降级报告。请重新评估后再启动进化"
            ),
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

    # 落库 session
    ev_db.create_session(session_id, case_id="")

    # 构建上下文：加载评估报告快照到 ctx.eval_snapshot（S2 DB 交接）+ 注入 recorder（D6）
    ctx = EvolveContext(session_id=session_id)
    ctx.recorder = get_recorder()
    ctx.trace_id = req.trace_id
    # 数据闭环 F1：查 trace 所属数据集层（golden验证/growing探索），注入进化上下文。
    ctx.origin_layer = _resolve_origin_layer(req.trace_id)
    ctx.eval_snapshot = {
        "eval_id": eval_session["eval_id"],
        "trace_id": eval_session.get("trace_id"),
        "scores": eval_session.get("scores"),
        "findings": eval_session.get("findings"),
        "report_md": eval_session.get("report_md"),
    }

    # 关联评估报告（eval_ref）
    ev_db.update_session(session_id, eval_ref=eval_session["eval_id"])

    # 后台跑进化驱动器（create_task 拿到 task 引用，存注册表供 stop 端点取消）
    task = asyncio.create_task(_run_evolve_bg(ctx, req.trace_id))
    _running_tasks[session_id] = task

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
    """手动停止运行中的进化 session（task.cancel → CancelledError 中断 ainvoke）。

    非阻塞：只取消 task + 标 cancelled，不等 Agent 真正退出。
    真正的状态收尾（recorder.cancel_run + DB）由 run_evolve_session 的
    CancelledError 分支处理。

    已知边界：Agent 若停在改源码中途，harnesses/current/ 下可能留脏文件，
    本端点不清理（由用户手动 stash / 重置）。
    """
    session = ev_db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")
    if session.get("status") != "running":
        raise HTTPException(
            status_code=400,
            detail=f"session 状态为 {session.get('status')}，只有 running 可停止",
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
    """trace 事件 → 前端 SSE 帧派生（与 eval_agent/api.py 对称）。"""
    if event.type == "run_meta" and event.input:
        data = event.input if isinstance(event.input, dict) else {}
        if "message" in data and "tool" not in data:
            return {"type": "log", "message": data["message"]}
        if "tool" in data:
            return {"type": "step", **data}
    return None


# ── 发版 / 丢弃（Phase 4，S9/S12）────────────────────────────


def _save_session_intent(version: int, session: dict) -> None:
    """从 design_doc 提取改动意图，存入 version_changes 版本级行（D-T1）。

    design_doc 不存在/解析失败 → 跳过（不阻断发版）。
    """
    from app.evolve.docs import parse_design_doc_intent
    from app.versioning import version_changes_repo

    design_doc_path = session.get("design_doc_path")
    if not design_doc_path:
        logger.info("session 无 design_doc_path，跳过意图提取（v%s）", version)
        return

    try:
        intent = parse_design_doc_intent(design_doc_path)
        if intent:
            version_changes_repo.save_intent(version, intent)
            logger.info("v%s 意图提取完成：%d 条改动", version, len(intent))
        else:
            logger.info("v%s design_doc 无 changes，跳过意图存储", version)
    except Exception:
        logger.exception("v%s 意图提取失败（不阻断发版）", version)


def _load_session_edits(session: dict) -> list[dict] | None:
    """读取 session 的配置层 edits.json。

    路径 = evolve_workspace/<session_id>/edits.json（与 ctx._edits_path / design_doc 同目录）。
    - 文件不存在 → None（纯源码层改动的 session，发版用 baseline config）。
    - 空数组 → None。
    - 解析/格式异常 → raise（发版失败，避免静默丢弃导致 v1==v2）。
    """
    from app.evolve.docs import session_dir

    session_id = session.get("session_id")
    if not session_id:
        return None

    edits_path = session_dir(session_id) / "edits.json"
    if not edits_path.exists():
        logger.info("session %s 无 edits.json，发版用 baseline config", session_id)
        return None

    try:
        edits = json.loads(edits_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"edits.json 解析失败（{edits_path}）：{e}") from e

    if not isinstance(edits, list):
        raise ValueError(f"edits.json 必须是数组（{edits_path}），得到 {type(edits).__name__}")
    if not edits:
        logger.info("session %s 的 edits.json 为空，发版用 baseline config", session_id)
        return None
    return edits


@router.post("/evolve/sessions/{session_id}/publish")
def publish_session(session_id: str) -> dict[str, Any]:
    """发版：把进化改动固化为新 Agent 版本（S9/S12）。

    流程：
      1. 校验 session 状态为 pending_review
      2. git commit + push（产 source_commit）
      3. 构建 config：bootstrap baseline + 回放 session 的配置层 edits.json
      4. publish_config（存 harness_snapshots 新 production + 旧 production 降 retired
         + 算 config diff 存 version_changes）
      5. 通知执行端
      6. 提取 design_doc 意图，存 version_changes 版本级行
      7. 推进 status → published（working 区解锁）
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
    from app.harness_config import edits as edit_ops
    from app.harness_config.bootstrap import build_v1_config
    from app.versioning import snapshot_repo
    from app.versioning.snapshot_publisher import notify_executor

    try:
        # 1. git commit + push → source_commit（execute 的源码层改动此时已落盘到 harnesses/current/）
        commit_msg = f"进化发版: session={session_id} trace={session.get('baseline_trace', '')}"
        source_commit = git_ops.commit_and_push(commit_msg)

        # 2. 构建 config：bootstrap baseline + 回放 session 的配置层 edits.json。
        # 源码层改动（write_file 落盘的 .py）已被 bootstrap 读到；配置层改动
        # （prompt slot / processor 装配）靠这里的 edits 回放。此前缺失回放，
        # 发版永远产出与上一版逐字节相同的 config（v1==v2 的根因）。
        base = build_v1_config()
        edits = _load_session_edits(session)
        if edits:
            config = edit_ops.apply_edits(base, edits)
            edits_applied = len(edits)
        else:
            config = base
            edits_applied = 0

        # 3. publish_config（存快照 + 旧 production 降 retired + 算 diff 存 version_changes）
        snapshot = snapshot_repo.publish_config(
            config,
            source_commit=source_commit,
            change_summary=f"进化 session {session_id} 产出的改动",
            source_session=session_id,
        )

        # 4. 通知执行端
        notified = notify_executor(snapshot["version"])

        # 5. 提取 design_doc 意图，存 version_changes 版本级行（版本差异展示 D-T1）
        _save_session_intent(snapshot["version"], session)

        # 6. 推进状态
        ev_db.update_session(session_id, status="published")

        logger.info(
            "进化发版成功: session=%s snapshot_v=%s commit=%s edits_applied=%s",
            session_id, snapshot["version"], source_commit, edits_applied,
        )
        return {
            "status": "published",
            "snapshot_version": snapshot["version"],
            "source_commit": source_commit,
            "notified": notified,
            "edits_applied": edits_applied,
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
