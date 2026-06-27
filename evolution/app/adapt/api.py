"""adapt API —— 触发 + 查询 + 控制 adaptation loop（Phase 8，前端驾驶舱后端）。

端点：
  POST /api/adapt/start          触发一轮 adapt session（异步，返回 session_id）
  GET  /api/adapt/sessions       session 列表（活动 + 历史，分页）
  GET  /api/adapt/sessions/{id}  单 session 详情（含历轮 adapt_rounds）
  GET  /api/adapt/sessions/{id}/stream   SSE 实时事件流
  POST /api/adapt/sessions/{id}/stop     软停（D12，loop_control 下轮生效）

执行模型（D4 实时 SSE）：
  start 时注册事件总线 → 后台 task 用 graph.astream() 流式执行 →
  每个节点产出 emit 到队列 → SSE 端点消费队列推给前端。
  进程重启则进行中 session 丢失（A12a 已接受）。

设计依据：需求基准 §4.3 + A12a + E4a。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.adapt import events
from app.adapt.batch import load_batch
from app.adapt.state import initial_state
from app.core import llm
from app.improvement.snapshot_repo import get_production_snapshot

logger = logging.getLogger("evolution.adapt.api")

router = APIRouter(tags=["adapt"])


class AdaptStartRequest(BaseModel):
    """adapt 启动请求（A12a + D6 可调参数）。"""

    rounds: int = 3       # T（A11b，默认 3）
    patience: int = 2     # P（A11b，默认 2）
    judge_j: int = 3      # verifier 打分次数（A3b，默认 3）


class AdaptStartResponse(BaseModel):
    session_id: str
    baseline_version: int
    batch_size: int
    status: str  # started


# ── 触发 ────────────────────────────────────────────────────


@router.post("/adapt/start", response_model=AdaptStartResponse, status_code=202)
async def adapt_start(
    req: AdaptStartRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> AdaptStartResponse:
    """手动触发一轮 adapt session（A12a）。

    异步启动 graph，立即返回 session_id。执行走 astream + 事件总线（D4）。
    """
    if not llm.judge_enabled():
        logger.warning("adapt 启动但 LLM judge 未配置，verifier/planner/evolver/critic 将降级")

    # 1. 读当前 production config 作基准（E6a）
    prod = get_production_snapshot()
    if prod is None:
        logger.info("无 production 快照，用 bootstrap 生成 v1")
        from app.compose.bootstrap import build_v1_config
        from app.improvement.snapshot_repo import publish_config
        from app.compose.git_ops import current_commit
        config = build_v1_config()
        prod = publish_config(
            config, source_commit=current_commit(),
            change_summary="adapt 首次启动：bootstrap v1",
        )

    baseline_config = json.loads(prod["config_json"])
    baseline_version = prod["version"]

    # 2. 加载 batch（A2a）
    batch = load_batch()

    # 3. 构建初始 state
    session_id = uuid.uuid4().hex[:12]
    state = initial_state(
        session_id=session_id,
        batch=batch,
        baseline_config=baseline_config,
        baseline_version=baseline_version,
        max_rounds=req.rounds,
        patience=req.patience,
        judge_j=req.judge_j,
    )

    # 4. 注册事件总线（先注册，确保 start 返回前 SSE 端点已能拿到队列）
    await events.register(session_id)

    logger.info(
        "adapt session %s 启动: baseline=v%d, batch=%d, rounds=%d, patience=%d",
        session_id, baseline_version, len(batch), req.rounds, req.patience,
    )

    # 5. 后台跑 graph（astream 流式 + emit 事件，D4）
    background_tasks.add_task(_run_adapt_session, state)

    return AdaptStartResponse(
        session_id=session_id,
        baseline_version=baseline_version,
        batch_size=len(batch),
        status="started",
    )


async def _run_adapt_session(state: dict[str, Any]) -> None:
    """后台执行 adapt session：astream 流式 + 事件总线 emit（D4）。

    graph 节点产出 → emit node_output；每轮 ship/loop_control → emit round_end；
    结束 → emit session_end；异常 → emit error 并标记终止（D10）。
    """
    from app.adapt.graph import build_adapt_graph

    session_id = state["session_id"]
    ev = events.get(session_id)
    last_round = state.get("round", 0)
    last_outcome = ""

    try:
        graph = build_adapt_graph()

        # astream 逐节点吐 state delta；node 是节点名，chunk 是该节点返回的更新
        async for node, chunk in graph.astream(
            state, {"recursion_limit": 100}, stream_mode="updates"
        ):
            # node_output：节点产出（landscape/edits/scores/verdict 等）
            _emit_node_output(ev, node, chunk, state)

            # 检测轮次推进 / 轮结果（loop_control 节点产出 round 增量）
            if node == "loop_control":
                new_round = chunk.get("round", last_round)
                if new_round != last_round or chunk.get("finished"):
                    _emit_round_end(ev, state, last_outcome)
                    last_round = new_round

            # 检测轮结果（gate 产出 round_outcome）
            if node == "gate":
                last_outcome = chunk.get("round_outcome", "")

            # 软停检查（D12）
            if events.is_stop_requested(session_id) and node in ("loop_control", "gate"):
                logger.info("session %s 软停生效", session_id)
                ev.finish("terminated", "用户软停")
                break

        logger.info(
            "adapt session %s 完成: rounds=%d, best_reward=%.3f",
            session_id, last_round, state.get("best_reward", 0),
        )
        ev.finish("completed")

    except Exception as exc:
        logger.exception("adapt session %s 执行失败", session_id)
        ev.finish("error", str(exc))


def _emit_node_output(
    ev: events.SessionEvents | None,
    node: str,
    chunk: dict[str, Any],
    state: dict[str, Any],
) -> None:
    """把节点产出转成前端可消费的事件 emit 到队列。"""
    if ev is None or not chunk:
        return

    round_num = state.get("round", chunk.get("round", 0))

    # 按节点挑出前端关心的 payload（不全推，避免噪音）
    payload: dict[str, Any] = {}
    if node == "run_baseline":
        payload = {"baseline_traces": chunk.get("baseline_traces", []),
                   "baseline_scores": chunk.get("baseline_scores", {})}
    elif node == "planner":
        payload = {"landscape": chunk.get("landscape", "")}
    elif node == "evolver":
        # 候选摘要（edits + manifest，不含完整 config）
        cands = chunk.get("candidates", [])
        payload = {"candidates": [
            {"edits": c.get("edits", []), "source_commit": c.get("source_commit", "")}
            for c in cands
        ]}
    elif node == "evaluate":
        payload = {"candidate_results": chunk.get("candidate_results", []),
                   "baseline_scores": chunk.get("baseline_scores", state.get("baseline_scores", {})),
                   "baseline_reward": chunk.get("baseline_reward", state.get("baseline_reward", 0))}
    elif node == "critic":
        payload = {"critic_verdict": chunk.get("critic_verdict", {})}
    elif node == "gate":
        payload = {"round_outcome": chunk.get("round_outcome", "")}
    elif node == "ship":
        # ship 后从 DB 读新版本号太重，前端可从 round_end 的 shipped_version 拿
        payload = {"shipped": True}
    elif node == "loop_control":
        payload = {"round": chunk.get("round"), "finished": chunk.get("finished"),
                   "best_reward": chunk.get("best_reward"), "idle_count": chunk.get("idle_count")}

    ev.emit({"type": "node_output", "node": node, "round": round_num, "payload": payload})


def _emit_round_end(
    ev: events.SessionEvents | None,
    state: dict[str, Any],
    outcome: str,
) -> None:
    """轮结束时 emit round_end（含 ship 的版本号）。"""
    if ev is None:
        return
    shipped_version = None
    # 从最新 adapt_rounds 行取 shipped_version（ship 节点已落库）
    import app.core.db as db
    row = db.query_one(
        "SELECT shipped_version FROM adapt_rounds WHERE session_id=? ORDER BY round DESC LIMIT 1",
        (state["session_id"],),
    )
    if row:
        shipped_version = row["shipped_version"]
    ev.emit({
        "type": "round_end", "round": state.get("round", 0),
        "outcome": outcome, "shipped_version": shipped_version,
    })


# ── 查询 ────────────────────────────────────────────────────


@router.get("/adapt/sessions")
async def list_sessions(
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """session 列表（D7 首页 + 驾驶舱入口）。

    合并两源：
      1. 事件总线内存态（进行中 / 当次进程内已终结）→ 实时状态
      2. adapt_rounds 表（持久化历史）→ 完整历史

    单人内部工具（D13），无归属过滤。
    """
    import app.core.db as db

    # 历史去重：按 session_id 聚合 adapt_rounds
    rows = db.query_all(
        """SELECT session_id,
                  MIN(round) AS first_round,
                  MAX(round) AS last_round,
                  COUNT(*) AS round_count,
                  SUM(CASE WHEN round_outcome='shipped' THEN 1 ELSE 0 END) AS shipped_count,
                  MAX(created_at) AS last_at,
                  MIN(created_at) AS started_at,
                  MAX(baseline_version) AS baseline_version,
                  MAX(shipped_version) AS shipped_version
           FROM adapt_rounds
           GROUP BY session_id
           ORDER BY last_at DESC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    )

    sessions = []
    active_ids = set(events.list_active())
    all_live = set(events.list_all())

    for r in rows:
        sid = r["session_id"]
        ev = events.get(sid)
        # 状态优先级：内存态 > 推断
        if ev and ev.terminal:
            status = ev.terminal  # completed/terminated/error
        elif sid in active_ids:
            status = "running"
        elif sid in all_live:
            status = "completed"
        else:
            # 无内存态：从轮数据推断（有 shipped=completed，否则=terminated）
            status = "completed" if r["shipped_count"] > 0 else "terminated"

        sessions.append({
            "session_id": sid,
            "status": status,
            "round_count": r["round_count"],
            "shipped_count": r["shipped_count"],
            "baseline_version": r["baseline_version"],
            "shipped_version": r["shipped_version"],
            "started_at": r["started_at"],
            "last_at": r["last_at"],
        })

    total = db.query_one("SELECT COUNT(DISTINCT session_id) AS n FROM adapt_rounds")["n"]

    return {"items": sessions, "total": total, "limit": limit, "offset": offset}


@router.get("/adapt/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    """单 session 详情（驾驶舱初始态 + 历轮结果）。"""
    import app.core.db as db

    rounds = db.query_all(
        """SELECT round, landscape, candidates_json, round_outcome,
                  shipped_version, baseline_version, baseline_scores,
                  candidate_scores, critic_verdict, created_at
           FROM adapt_rounds WHERE session_id=? ORDER BY round""",
        (session_id,),
    )
    if not rounds:
        # 可能是刚启动还没落任何轮的 session
        ev = events.get(session_id)
        if ev is None:
            raise HTTPException(404, "session 不存在")
        return {"session_id": session_id, "status": "running" if ev.terminal is None else ev.terminal,
                "rounds": [], "config": {"rounds": 0, "patience": 0, "judge_j": 0}}

    ev = events.get(session_id)
    if ev and ev.terminal:
        status = ev.terminal
    elif session_id in set(events.list_active()):
        status = "running"
    else:
        status = "completed" if any(r["round_outcome"] == "shipped" for r in rounds) else "terminated"

    parsed_rounds = []
    for r in rounds:
        parsed_rounds.append({
            "round": r["round"],
            "landscape": r["landscape"],
            "candidates": json.loads(r["candidates_json"]) if r["candidates_json"] else [],
            "round_outcome": r["round_outcome"],
            "shipped_version": r["shipped_version"],
            "baseline_version": r["baseline_version"],
            "baseline_scores": json.loads(r["baseline_scores"]) if r["baseline_scores"] else {},
            "candidate_scores": json.loads(r["candidate_scores"]) if r["candidate_scores"] else [],
            "critic_verdict": json.loads(r["critic_verdict"]) if r["critic_verdict"] else {},
            "created_at": r["created_at"],
        })

    return {
        "session_id": session_id,
        "status": status,
        "rounds": parsed_rounds,
        "baseline_version": rounds[0]["baseline_version"],
    }


# ── SSE 流（D4）────────────────────────────────────────────


@router.get("/adapt/sessions/{session_id}/stream")
async def stream_session(session_id: str, request: Request) -> StreamingResponse:
    """SSE 实时事件流（D4）。消费事件总线队列。

    若 session 已终结，先 flush 一个 session_end 再关闭（让前端拿到终态）。
    若 session 不存在，立即返回 404 文本流。
    """
    ev = events.get(session_id)
    if ev is None:
        # 不在内存：可能从未存在，或进程重启后丢失。检查是否有历史轮。
        import app.core.db as db
        row = db.query_one(
            "SELECT 1 FROM adapt_rounds WHERE session_id=? LIMIT 1", (session_id,)
        )
        if row:
            # 有历史但无内存态 = 进程重启后丢失的 session
            async def _dead() -> Any:
                yield 'event: session_end\ndata: {"outcome":"terminated","reason":"进程重启，session 状态丢失"}\n\n'
            return StreamingResponse(_dead(), media_type="text/event-stream")
        raise HTTPException(404, "session 不存在")

    async def event_generator() -> Any:
        # 先发一个 hello（前端确认连接建立 + 当前状态）
        hello = {"type": "session_hello", "session_id": session_id,
                 "terminal": ev.terminal}
        yield f"data: {json.dumps(hello, ensure_ascii=False)}\n\n"

        # 已终结：补发终态事件后结束
        if ev.terminal:
            term = {"type": "session_end" if ev.terminal == "completed" else "error",
                    "outcome": ev.terminal, "reason": ev.terminal_reason}
            yield f"data: {json.dumps(term, ensure_ascii=False)}\n\n"
            return

        # 实时消费队列
        try:
            while True:
                # 客户端断开检测
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(ev.queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 心跳：保持连接活跃 + 让前端知道还活着
                    yield ": keep-alive\n\n"
                    continue

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

                # 终态事件推完即结束流
                if event.get("type") in ("session_end", "error"):
                    break
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 软停（D12）─────────────────────────────────────────────


@router.post("/adapt/sessions/{session_id}/stop")
async def stop_session(session_id: str) -> dict[str, str]:
    """请求软停（D12）。设置标志位，loop_control 下轮检查时终止。"""
    if not events.request_stop(session_id):
        raise HTTPException(409, "session 不存在或已终结，无法停止")
    return {"session_id": session_id, "status": "stop_requested"}
