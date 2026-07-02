"""internal 诊断路由（Phase 6 T15 + Phase 3 T3.1 + 重构 Phase 3）。

供 evolution：
  - GET  /internal/active-runs：轮询拉取活跃 trace 列表（活跃大盘）
  - GET  /internal/traces/{trace_id}：拉取 trace 完整内容（run 摘要 + 事件列表）
  - GET  /internal/traces?since=：兜底拉取近期 trace 列表
  - POST /internal/ab-replay：A/B 回放——用指定 prompt label 跑一次生成
    （trace 标 run_purpose=optimization，evolution 断路不进优化池）

内部接口，无鉴权（evolution 与执行端同信任域），不暴露给终端用户。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from contracts.api import TraceContentResponse, TraceListItem, TraceListResponse, PromptRefreshNotice
from app.routers.context import get_agent_service, get_thread_store, get_trace_recorder

logger = logging.getLogger("writer.internal")

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)

# A/B 回放专用系统账号（不污染用户数据，trace 走独立 workspace）
AB_REPLAY_OWNER = "ab-replay"


@router.get("/active-runs")
def active_runs() -> list[dict[str, Any]]:
    """当前活跃 trace 列表（T15 活跃大盘）。

    evolution 定期轮询此端点，展示"哪些 trace 在跑、跑了多久"。
    纯内存读取，不涉及文件 IO。
    """
    return get_trace_recorder().list_active_runs()


# ── Phase 3 T3.1：A/B 回放端点 ──────────────────────────────


class ABReplayRequest(BaseModel):
    """A/B 回放请求（evolution 的 experiment.py 调用）。"""

    prompt_label: str  # 用哪个 label 的 prompt 跑（production / candidate）
    genre: str = "玄幻"  # 创作品类（A/B 测试集需求）
    premise: str = ""  # 创作前提/需求描述
    title: str = "A/B回放测试"  # workspace 标题


class ABReplayResponse(BaseModel):
    """A/B 回放响应。"""

    trace_id: str
    workspace_id: str
    thread_id: str
    status: str  # completed / failed
    error: str | None = None


@router.post("/ab-replay", response_model=ABReplayResponse)
async def ab_replay(req: ABReplayRequest) -> ABReplayResponse:
    """A/B 回放：用指定 prompt label 跑一次完整生成（D5 复用生成链路）。

    流程：
      1. 建独立 workspace + thread（AB_REPLAY_OWNER，不污染用户数据）
      2. set prompt label override（contextvar，让生成链路用 req.prompt_label）
      3. 跑 generate_stream（run_purpose=optimization，trace 标断路标记）
      4. 消费整个 SSE 流等生成完成，返回 trace_id

    trace 标 run_purpose=optimization → evolution 摄入但断路不进优化池（防自指）。
    """
    from app.platform.prompt.loader import (
        reset_prompt_label_override,
        set_prompt_label_override,
    )
    from app.schemas.screenplay import ScreenplayGenerateRequest

    thread_store = get_thread_store()
    agent_service = get_agent_service()

    # 1. 建独立 workspace + thread
    run_tag = uuid.uuid4().hex[:8]
    ws = thread_store.create_workspace(
        AB_REPLAY_OWNER, f"{req.title}-{run_tag}", "writing"
    )
    thread = thread_store.create_thread(
        AB_REPLAY_OWNER, ws.workspace_id, f"ab-replay-{run_tag}"
    )

    # 2. 构造生成请求
    payload = ScreenplayGenerateRequest(
        prompt=req.premise or f"写一部{req.genre}小说",
        genre=req.genre,
        premise=req.premise,
        title=req.title,
    )

    # 3. set prompt label override + 跑生成
    token = set_prompt_label_override(req.prompt_label)
    trace_id = ""
    status = "completed"
    error: str | None = None
    try:
        async for event in agent_service.generate_stream(
            payload, thread, owner_id=AB_REPLAY_OWNER, run_purpose="optimization"
        ):
            # 消费 SSE 流；从 status 事件取 trace_id
            if event.startswith("event: status") or '"trace_id"' in event:
                import re

                m = re.search(r'"trace_id"\s*:\s*"([^"]+)"', event)
                if m:
                    trace_id = m.group(1)
    except Exception as exc:
        logger.exception("A/B 回放生成失败")
        status = "failed"
        error = f"{exc.__class__.__name__}: {exc}"
    finally:
        reset_prompt_label_override(token)

    # 兜底：若没从事件取到 trace_id，从 recorder 查最近一次
    if not trace_id:
        recent = [
            r for r in get_trace_recorder().list_active_runs()
            if r.get("endpoint") == "screenplay.generate.stream"
        ]
        if recent:
            trace_id = recent[-1]["trace_id"]

    return ABReplayResponse(
        trace_id=trace_id, workspace_id=ws.workspace_id,
        thread_id=thread.thread_id, status=status, error=error,
    )


# ── 重构 Phase 3：trace 内容拉取（替代 evolution 读文件系统）──


@router.get(
    "/traces/{trace_id}",
    response_model=TraceContentResponse,
    responses={404: {"description": "trace_id 未找到（索引丢失或 trace 不存在）"}},
)
def get_trace_content(
    trace_id: str,
    since_seq: int = Query(0, description="只返回 sequence > since_seq 的事件（增量拉取，D8）"),
) -> TraceContentResponse:
    """拉取 trace 完整内容（run 摘要 + 事件列表）。

    evolution 收到 trace 完成通知后调此端点，替代旧的「传文件路径让 evolution
    读文件」的耦合方式。依赖 recorder 的 _trace_workspace 索引定位 workspace。

    since_seq（D8 增量）：只返回 sequence > since_seq 的事件。run 摘要始终全量返回
    （含最新 status/event_count），evolution 据此更新 runs 表。0 = 全量事件。

    404 场景：trace_id 不在索引中（进程重启导致索引丢失），evolution 应靠
    scan 兜底（GET /internal/traces）补拉。
    """
    recorder = get_trace_recorder()
    run = recorder.find_run_by_trace_id(trace_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"trace_id not found: {trace_id}")
    events = recorder.read_trace_events(trace_id, since_seq=since_seq)
    if events is None:
        raise HTTPException(status_code=404, detail=f"trace file missing: {trace_id}")
    return TraceContentResponse(run=run, events=events)


@router.get("/traces", response_model=TraceListResponse)
def list_traces(since: str = Query("", description="ISO 时间戳，只返回此时间之后的 trace")) -> TraceListResponse:
    """列出近期 trace（evolution scan 兜底用）。

    返回本进程生命周期内创建的 trace 清单。进程重启后索引不全，
    仅覆盖重启后的 trace——这是设计取舍（全量扫描 workspace 成本太高）。
    """
    items = [
        TraceListItem(**item)
        for item in get_trace_recorder().list_recent_runs(since)
    ]
    return TraceListResponse(traces=items)


# ── 重构 Phase 5：prompt 更新通知（D7 方案B）──


@router.post("/prompts/refreshed")
def prompt_refreshed(notice: PromptRefreshNotice) -> dict[str, str]:
    """evolution 通知执行端「有新 prompt 版本上线」。

    evolution 给某 prompt 版本打上 production label 后，发此通知。
    执行端收到后标记对应缓存为 stale，下次 load_prompt 时重新从 evolution 拉取。

    只带标识，不带内容——内容仍由执行端主动拉取（D7 方案B 设计）。
    幂等：重复通知无害（mark_stale 是集合操作）。
    """
    from app.platform.prompt.loader import get_loader
    get_loader().mark_stale(notice.name, notice.label)
    logger.info("prompt %s (label=%s) 标记 stale，下次 load 时重拉", notice.name, notice.label)
    return {"status": "ok", "name": notice.name, "label": notice.label}


@router.post("/snapshot/refreshed")
def snapshot_refreshed(body: "SnapshotRefreshNotice") -> dict[str, Any]:
    """evolution 通知执行端「有新 production 快照发布」（Phase 7 T5.4）。

    evolution 发布新快照后（snapshot_publisher.notify_executor），发此通知。

    Phase 7 语义：执行端的 Agent 包是进程级缓存（package_loader._loaded_package），
    换版本需重启进程（D11 设计）。本端点只记录日志——真正生效靠下次进程重启
    重新 load_current_package 加载新包内容。

    幂等：重复通知无害（仅记日志）。
    替代 Phase 6 的 /manifest/refreshed（包化取代 manifest 指针）。
    """
    from app.platform.agent.loader import reset_cache
    reset_cache()  # 清缓存，下次 load_current_package 重新加载（同进程内热更新）
    logger.info("快照 v%s 通知：包缓存已清，下次 load 重载", body.snapshot_version)
    return {"status": "ok", "snapshot_version": body.snapshot_version}


class SnapshotRefreshNotice(BaseModel):
    """快照变更通知 body（evolution → 执行端，Phase 7）。"""

    snapshot_version: int


# ── Phase 8 compose：热加载 + 候选执行端点（决策 #16/D7a/E5a）──


@router.post("/reload")
def reload_harness() -> dict[str, Any]:
    """热加载：git pull + 重新加载生产包（决策 #16，不重启进程）。

    evolution ship 新 config + commit 后调此端点。
    executor git pull 最新 main → reload_current() 重新加载包。

    注意：本端点只重新加载「包模块」。assemble 需要新 config 才会用配置驱动——
    生产路径的 config 由调用方（agent_service）从 evolution 拉 production config 提供。
    本端点确保包源码是最新的（git pull），config 由生成请求时获取。
    """
    from app.platform.agent.loader import reload_current

    pkg = reload_current()
    from app.platform.agent.git_sync import production_commit
    commit = production_commit()
    logger.info("harness 热加载完成: commit=%s", commit)
    return {"status": "reloaded", "commit": commit}


class ABRunRequest(BaseModel):
    """候选执行请求（evolve 的 run_baseline/run_candidate 工具调用，D2 同进程热加载）。

    字段对齐新设计（.claude/md/20260627_135113）：
    - config：候选 HarnessConfig JSON（baseline=True 时可省略，用硬编码）
    - demand_md：预置 demand.md 内容（interview 直通用）
    - baseline：True=跑当前 Agent（无 config），False=跑进化后 Agent（用 config）
    - source_commit：快照版本 git commit；None=用 harnesses/current（working 包）
    """
    config: dict | None = None  # 候选 HarnessConfig JSON（baseline=True 时可省略）
    demand_md: str = ""  # 预置 demand.md 内容（interview 直通）
    baseline: bool = True  # True=当前 Agent，False=候选 Agent
    source_commit: str | None = None  # 快照版本 git commit；None=working 包（harnesses/current）


class ABRunResponse(BaseModel):
    """候选执行响应（异步任务，立即返回 task_id）。"""

    task_id: str


@router.post("/ab/run", response_model=ABRunResponse, status_code=202)
async def ab_run(req: ABRunRequest, background_tasks: BackgroundTasks) -> ABRunResponse:
    """启动候选执行（异步，D2 同进程热加载）。

    立即返回 task_id，executor 后台跑：
      1. 准备隔离 workspace + 写 demand.md（interview 直通）
      2. importlib 加载 source_root（同进程热加载）
      3. assemble(ctx, config, source_root) 跑生成
      4. 存 trace_ids 到 _ab_tasks，供 /ab/status 轮询

    evolution 的 run_baseline/run_candidate 工具轮询 /ab/status/{task_id} 直到 done。
    """
    import threading
    import uuid

    task_id = uuid.uuid4().hex[:12]
    _ab_tasks[task_id] = {
        "status": "running",
        "trace_ids": [],
        "error": None,
        # 取消标志：stop 端点 set() 后，_execute_ab 在 super-step 边界中断
        "cancel_event": threading.Event(),
    }
    logger.info(
        "候选执行任务启动: task=%s, baseline=%s",
        task_id, req.baseline,
    )

    # 后台执行
    background_tasks.add_task(_execute_ab, task_id, req)
    return ABRunResponse(task_id=task_id)


def _execute_ab(task_id: str, req: "ABRunRequest") -> None:
    """后台执行 A/B 生成（同步阻塞跑完，写结果到 _ab_tasks）。"""
    # 取消标志（ab_run 端点创建，run_ab_generation 在 super-step 边界检查）
    task_state = _ab_tasks.get(task_id) or {}
    cancel_event = task_state.get("cancel_event")
    # source_root：快照版本按 source_commit checkout；working 包用 harnesses/current
    checked_out: Path | None = None
    try:
        from app.routers.ab_endpoint import run_ab_generation
        from app.platform.core.settings import get_settings as _get_writer_settings
        from app.routers.context import get_trace_recorder

        writer_settings = _get_writer_settings()
        trace_recorder = get_trace_recorder()
        if req.source_commit:
            # 快照版本：clone bare repo + checkout 指定 commit 到临时目录
            from app.platform.agent.git_sync import checkout_commit, cleanup_checkout

            source_root = checkout_commit(req.source_commit)
            checked_out = source_root
            logger.info("快照执行: task=%s commit=%s → %s", task_id, req.source_commit, source_root)
        else:
            # working 包：harness 包工作目录（生产路径 current）
            source_root = Path(writer_settings.harness_package_path).resolve()
            if not source_root.exists():
                # 回退：从 evolution 工作目录找
                source_root = Path(__file__).resolve().parents[3] / "evolution" / "harnesses" / "current"

        trace_id = run_ab_generation(
            config=req.config if not req.baseline else None,
            source_root=source_root,
            demand_md=req.demand_md,
            trace_recorder=trace_recorder,
            writer_settings=writer_settings,
            on_trace_created=lambda tid: _ab_tasks.update(
                {task_id: {"status": "running", "trace_ids": [tid], "error": None,
                           "cancel_event": cancel_event}}
            ),
            cancel_event=cancel_event,
        )
        # run_ab_generation 在 cancelled 时已调 cancel_run 收尾；这里区分终态
        if cancel_event.is_set():
            _ab_tasks[task_id] = {
                "status": "cancelled", "trace_ids": [trace_id], "error": None,
                "cancel_event": cancel_event,
            }
            logger.info("候选执行任务被停止: task=%s trace=%s", task_id, trace_id)
        else:
            _ab_tasks[task_id] = {"status": "done", "trace_ids": [trace_id], "error": None,
                                  "cancel_event": cancel_event}
            logger.info("候选执行任务完成: task=%s trace=%s", task_id, trace_id)
    except BaseException as exc:
        logger.exception("候选执行任务失败: task=%s", task_id)
        _ab_tasks[task_id] = {"status": "failed", "trace_ids": [], "error": str(exc),
                              "cancel_event": cancel_event}
        # 失败兜底通知 evolution（trace 可能尚未创建，ingest 链路收不到）
        _notify_evolution_task_failed(task_id, str(exc))
    finally:
        if checked_out is not None:
            from app.platform.agent.git_sync import cleanup_checkout

            cleanup_checkout(checked_out)


def _notify_evolution_task_failed(task_id: str, error: str) -> None:
    """任务在产出 trace 前就失败时，主动通知 evolution 按 task_id 标记测试记录 failed。

    与 _notify_evolution（recorder.py）平行：那条走 trace_id，这条走 task_id。
    纯副作用、彻底降级（fire-and-forget，异常静默）。
    """
    try:
        from app.platform.core.settings import get_settings
        from app.platform.trace.recorder import _EVOLUTION_NOTIFY_TIMEOUT

        url = get_settings().evolution_notify_url
        if not url:
            return
        import httpx

        httpx.post(
            url,
            json={
                "trace_id": "",  # 无 trace
                "task_id": task_id,
                "status": "failed",
                "error": error,
            },
            timeout=_EVOLUTION_NOTIFY_TIMEOUT,
        )
    except Exception:
        pass


@router.get("/ab/status/{task_id}")
def ab_status(task_id: str) -> dict[str, Any]:
    """查询候选执行任务状态（轮询）。

    Returns:
        {status: running/done/failed/cancelled, trace_ids: [...], error: ...}

    注意：只显式挑选可序列化字段。task 字典里还存了 cancel_event
    （threading.Event，内部含 _thread.lock，不可 JSON 序列化），若直接 return
    整个 task，jsonable_encoder 会抛异常导致端点 500，进化端轮询永远拿不到
    trace_id（表现为前端卡在"等待 executor 创建 trace…"）。
    """
    task = _ab_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    # cancel_event 是内部取消标志（threading.Event），不下发
    return {
        "status": task.get("status"),
        "trace_ids": task.get("trace_ids", []),
        "error": task.get("error"),
    }


@router.post("/ab/stop/{task_id}")
def ab_stop(task_id: str) -> dict[str, Any]:
    """请求停止运行中的候选执行任务（边界停）。

    set 取消标志 → _execute_ab 在下一个 super-step 边界中断 → trace 收尾 cancelled。
    不会立即中断（需等当前 LLM/节点周期结束），响应快但停止有数秒延迟。

    - task 不存在 → 404
    - task 已终态（done/failed/cancelled）→ 409，无需停止
    - task running → set 标志，返回 accepted
    """
    task = _ab_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    if task.get("status") != "running":
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} 已终态（{task.get('status')}），无需停止",
        )
    cancel_event = task.get("cancel_event")
    if cancel_event is None:
        # 老任务无取消标志（兼容）：无法停止
        raise HTTPException(status_code=409, detail=f"task {task_id} 不支持停止")
    cancel_event.set()
    logger.info("候选执行任务收到停止请求: task=%s", task_id)
    return {"status": "accepted", "task_id": task_id}


# 内存任务表（进程级。生产可换 Redis/DB）
_ab_tasks: dict[str, dict[str, Any]] = {}
