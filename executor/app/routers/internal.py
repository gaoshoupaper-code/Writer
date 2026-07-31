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


@router.get("/users")
def list_users_brief() -> list[dict[str, Any]]:
    """用户列表最小集（evolution 用户名映射同步用）。

    evolution 定时拉取此端点，把 user_id→username 映射同步到本地 user_cache 表，
    供 trace 历史列表展示用户名。只返回最小集，不含密码/key 等敏感字段。
    """
    from app.platform.core.db import UserRepository, get_database

    users = UserRepository(get_database())
    return [
        {
            "user_id": r["user_id"],
            "username": r["username"],
            "disabled": bool(r["disabled"]),
        }
        for r in users.list_all()
    ]


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
    status: str  # completed / failed / evidence_capture_failed
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


@router.get("/traces/{trace_id}/payloads/{payload_id}")
def get_trace_payload(trace_id: str, payload_id: str) -> Any:
    """受信任的 evolution 拉取入口；终端用户永远不直接访问正文。"""
    try:
        return get_trace_recorder().read_payload(trace_id, payload_id)
    except (KeyError, FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="trace payload not found")


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


class HarnessProbeRequest(BaseModel):
    source_commit: str


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
    from app.platform.agent.runtime_identity import build_runtime_identity
    from app.platform.agent.git_sync import production_checkout

    runtime_identity = build_runtime_identity(
        harness_root=production_checkout(), harness_commit=commit
    )
    logger.info("harness 热加载完成: commit=%s", commit)
    return {"status": "reloaded", "commit": commit, "runtime_identity": runtime_identity}


@router.post("/harness/probe")
def probe_harness(body: HarnessProbeRequest) -> dict[str, Any]:
    """从 candidate 干净 checkout 导入并真实编译最小 DeepAgent 图。"""
    import sys
    import tempfile
    import uuid
    from pathlib import Path

    from contracts.runtime_context import RuntimeContext
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.platform.agent.git_sync import checkout_commit, cleanup_checkout
    from app.platform.agent.loader import load_package
    from app.platform.agent.runtime import OverwritingFilesystemBackend, artifact_capture_scope
    from app.platform.agent.runtime_identity import build_runtime_identity

    class _ProbeRecorder:
        def record_artifact_revision(self, *_args, **_kwargs):
            return None

        def record_middleware_assembly(self, *_args, **_kwargs):
            return None

        def record_skill_catalog(self, *_args, **_kwargs):
            return None

    checkout = checkout_commit(body.source_commit)
    module_name = f"harness_probe_{uuid.uuid4().hex}"
    try:
        middleware_path = checkout / "middleware" / "artifact_snapshot.py"
        if not middleware_path.is_file():
            raise RuntimeError(
                f"candidate {body.source_commit} 缺少 middleware/artifact_snapshot.py"
            )
        package = load_package(checkout, module_name)
        recorder = _ProbeRecorder()
        with tempfile.TemporaryDirectory(prefix="harness_probe_workspace_") as tmp:
            workspace = Path(tmp)
            context = RuntimeContext(
                model=FakeListChatModel(responses=["ok"]),
                backend=OverwritingFilesystemBackend(root_dir=workspace, virtual_mode=True),
                checkpointer=None,
                workspace_path=workspace,
                trace_id="harness-probe",
                trace_recorder=recorder,
                artifact_snapshot_callback=lambda _data: None,
            )
            with artifact_capture_scope(
                recorder=recorder,
                trace_id="harness-probe",
                workspace_root=workspace,
                strict=True,
            ):
                graph = package.assemble(context)
        identity = build_runtime_identity(
            harness_root=checkout, harness_commit=body.source_commit
        )
        if identity["harness_dirty"]:
            raise RuntimeError(f"candidate {body.source_commit} checkout 不干净")
        return {
            "status": "ready",
            "assembled": graph is not None,
            "harness_commit": body.source_commit,
            "artifact_snapshot_middleware": True,
            "runtime_identity": identity,
        }
    finally:
        for name in list(sys.modules):
            if name == module_name or name.startswith(module_name + "."):
                sys.modules.pop(name, None)
        cleanup_checkout(checkout)


class ABRunRequest(BaseModel):
    """候选执行请求（evolve 的 run_baseline/run_candidate 工具调用，D2 同进程热加载）。

    - demand_md：预置 demand.md 内容（interview 直通用）
    - source_commit：快照版本 git commit；None=working 包（harnesses/repo）
    - baseline / config：遗留字段（去 DB 重构后不再驱动装配——
      候选/工作版本都走 assemble(ctx) 单参数契约，版本差异由 source_root 决定）。
      保留只为 HTTP 契约向后兼容，内部已忽略。
    - traceparent：可选 W3C traceparent（FR-004/DEC-005）。继承 evolution 发起链路
      的上游 context，透传到隔离子进程 trace。缺失时 executor 自行生成有效 context。
      兼容未传字段的既有调用方（普通创作 trace 行为不变）。
    - test_id：可选 evolution 单次测试 ID（FR-004）。opaque ID，写入 trace external_refs
      供跨服务追溯。仅允许非敏感 opaque ID，禁止正文/凭证（CON-003）。
    """
    config: dict | None = None  # 遗留字段，内部已忽略（保留向后兼容）
    demand_md: str = ""  # 预置 demand.md 内容（interview 直通）
    baseline: bool = True  # 遗留字段，内部已忽略（保留向后兼容）
    source_commit: str | None = None  # 快照版本 git commit；None=working 包（harnesses/repo）
    traceparent: str | None = None  # FR-004：W3C traceparent，继承上游 context
    test_id: str | None = None  # FR-004：evolution 单次测试 ID，写入 external_refs


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
    """后台执行 A/B 生成（隔离子进程，写结果到 _ab_tasks）。

    FR-006 / NFR-001：run_ab_generation 在独立子进程跑，取消时可在十秒内强杀，
    不受长 LLM / 长工具的 C 层阻塞影响。子进程内独立构建 recorder，trace 写到
    共享 workspace，通过 Queue 回传 trace_id。
    """
    task_state = _ab_tasks.get(task_id) or {}
    # source_root：快照版本按 source_commit checkout；working 包用 harnesses/current
    checked_out: Path | None = None
    worker = None
    try:
        from app.platform.core.settings import get_settings as _get_writer_settings
        from app.platform.isolation import IsolatedGenerationWorker

        writer_settings = _get_writer_settings()
        if req.source_commit:
            # 快照版本：clone bare repo + checkout 指定 commit 到临时目录
            from app.platform.agent.git_sync import checkout_commit

            source_root = checkout_commit(req.source_commit)
            checked_out = source_root
            logger.info("快照执行: task=%s commit=%s → %s", task_id, req.source_commit, source_root)
        else:
            # working 包：harness 包工作目录（生产路径 current）
            source_root = Path(writer_settings.harness_package_path).resolve()
            if not source_root.exists():
                # 回退：从 evolution 工作目录找
                source_root = Path(__file__).resolve().parents[3] / "evolution" / "harnesses" / "current"

        workspace_root = Path(writer_settings.workspace_root).resolve()
        worker = IsolatedGenerationWorker(
            source_root=source_root,
            demand_md=req.demand_md,
            workspace_root=workspace_root,
            traceparent=req.traceparent,
            test_id=req.test_id,
            task_id=task_id,
        )
        # 登记 worker 到 task 表（ab_stop 用它做硬终止）。
        _ab_tasks[task_id]["worker"] = worker
        worker.start()

        # 阻塞等子进程回传 trace_id（running 期间就能拿到）。
        trace_id = worker.wait_for_trace_id(timeout=300)
        if trace_id:
            _ab_tasks[task_id]["trace_ids"] = [trace_id]
            # FR-001 根因修复：把子进程 trace 的 workspace_path 登记进主 recorder，
            # 否则 GET /internal/traces/{trace_id} 会在主进程内存索引查无 → 稳定 404
            # （EVD-002/003）。子进程产物已落盘，主进程只需拿到定位信息即可读取。
            ws_path = worker.workspace_path
            if ws_path:
                main_recorder = get_trace_recorder()
                main_recorder.register_external_run(trace_id, ws_path)

        # 阻塞等子进程自然结束（非取消场景）。取消由 ab_stop 的后台收敛线程处理。
        # 用循环 + is_alive 检查，让取消收敛能更新状态。
        while worker.is_alive():
            import time
            time.sleep(0.5)
            # 若已被 ab_stop 标记 cancelling，说明取消收敛正在进行——本循环让路，
            # 不再尝试覆盖终态（避免与取消收敛线程竞争，CON-003 单调性）。
            if _ab_tasks.get(task_id, {}).get("status") == "cancelling":
                logger.info("task %s 已进入取消收敛，_execute_ab 退出等待", task_id)
                return

        # 收集终态（遵循单调终态规则，CON-003：不覆盖取消收敛已设的终态）。
        from contracts.cancel_state import can_transition_to

        worker._drain_queue()
        result = getattr(worker, "_last_result", None)
        current = _ab_tasks.get(task_id, {}).get("status")
        if result is not None and can_transition_to(current, result.status):
            _ab_tasks[task_id]["status"] = result.status
            if result.trace_id:
                _ab_tasks[task_id]["trace_ids"] = [result.trace_id]
            if result.error:
                _ab_tasks[task_id]["error"] = result.error
            if result.status in {"failed", "evidence_capture_failed"}:
                _notify_evolution_task_failed(task_id, result.error or "")
        logger.info(
            "候选执行任务结束: task=%s status=%s",
            task_id, _ab_tasks[task_id].get("status"),
        )
    except BaseException as exc:
        logger.exception("候选执行任务失败: task=%s", task_id)
        prior_trace_ids = (_ab_tasks.get(task_id) or {}).get("trace_ids", [])
        _ab_tasks[task_id]["status"] = "failed"
        _ab_tasks[task_id]["trace_ids"] = prior_trace_ids
        _ab_tasks[task_id]["error"] = str(exc)
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

        # 桌面化改造（2026-07-07）：内网通知带 X-Notify-Token（evolution NotifyTokenMiddleware 校验）
        notify_token = get_settings().evolution_notify_token
        headers = {"X-Notify-Token": notify_token} if notify_token else None

        httpx.post(
            url,
            json={
                "trace_id": "",  # 无 trace
                "task_id": task_id,
                "status": "failed",
                "error": error,
            },
            headers=headers,
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
    """请求停止运行中的候选执行任务（十秒硬终止，FR-006 / NFR-001 / DEC-002）。

    立即持久化取消身份 + 标 cancelling 并返回（DEC-002 立即反馈），后台线程在 10 秒
    时限内通过子进程协作取消 + 必要时强杀收敛，并让父进程接管 canonical Trace 封存
    （FR-002/006 根因修复：子进程强杀后不再留下永久 running/incomplete 孤儿）。

    - task 不存在 → 404
    - task 已终态（done/failed/cancelled/cancel_timeout）→ 409，幂等返回当前状态
    - task running/cancelling → 持久 cancel_id，标 cancelling，后台收敛，返回 cancelling
    - 重复请求（同一 task 已 cancelling）→ 幂等返回同一 cancel_id 与当前进度（FR-006）
    """
    from contracts.cancel_state import HARD_STOP_DEADLINE_SECONDS, is_terminal
    from contracts.trace import CancelAudit
    from uuid import uuid4

    task = _ab_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    current_status = task.get("status")
    if is_terminal(current_status):
        # 幂等：已终态返回当前进度（EDGE-003 重复取消）。
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} 已终态（{current_status}），无需停止",
        )

    # 幂等取消身份（FR-006）：同一 task 重复 stop 返回同一 cancel_id，不重复发起收敛。
    cancel_id = task.get("cancel_id")
    if cancel_id is None:
        cancel_id = f"cancel-{uuid4().hex}"
        task["cancel_id"] = cancel_id
        cancel_audit = CancelAudit(
            cancel_id=cancel_id,
            requested_by="user",
            requested_at=_now_iso(),
            reason="user_stop",
        )
        task["cancel_audit"] = cancel_audit
    else:
        cancel_audit = task["cancel_audit"]

    # 持久化"用户请求取消"事实到 canonical Trace（维度4 时间线起点，FR-006/CON-003）。
    # 取消意图此前只在内存，强杀/重启即丢；落成 cancel_requested 事件 + index 审计字段。
    trace_ids: list[str] = list(task.get("trace_ids") or [])
    main_recorder = get_trace_recorder()
    for trace_id in trace_ids:
        try:
            run = main_recorder.find_run_by_trace_id(trace_id)
            if run is not None:
                from app.schemas.screenplay import ThreadSummary
                thread = ThreadSummary(
                    thread_id=str(run.thread_id),
                    workspace_id=str(run.workspace_id),
                    session_name=str(run.session_name),
                    workspace_path=main_recorder._trace_workspace.get(trace_id) or run.workspace_path,
                    created_at=run.started_at,
                    updated_at=run.started_at,
                )
                main_recorder.record_cancel_requested(thread, trace_id, cancel_audit)
        except Exception:
            logger.warning("持久化 cancel_requested 失败: task=%s trace=%s", task_id, trace_id, exc_info=True)

    # 立即标记 cancelling（DEC-002：用户提交后当前帧可见）。
    _ab_tasks[task_id]["status"] = "cancelling"
    logger.info("候选执行任务进入取消中: task=%s cancel_id=%s", task_id, cancel_id)

    # 后台线程做硬终止收敛 + 父进程接管 canonical Trace 封存（不阻塞 HTTP 响应）。
    worker = task.get("worker")
    if worker is not None and not task.get("converge_started"):
        task["converge_started"] = True  # 防重复请求重复起收敛线程
        import threading

        def _converge_cancel() -> None:
            _run_cancel_convergence(task_id, worker, cancel_audit, main_recorder)

        threading.Thread(target=_converge_cancel, daemon=True).start()
    elif worker is None:
        # 无 worker（兼容老任务或 worker 未就绪）：回退协作式取消。
        cancel_event = task.get("cancel_event")
        if cancel_event is not None:
            cancel_event.set()
        _ab_tasks[task_id]["status"] = "cancelled"
        # 回填取消审计收敛结果。
        cancel_audit.converge_status = "cancelled"
        cancel_audit.converged_at = _now_iso()

    return {"status": "cancelling", "task_id": task_id, "cancel_id": cancel_id}


def _run_cancel_convergence(
    task_id: str,
    worker: Any,
    cancel_audit: Any,
    main_recorder: Any,
) -> None:
    """取消收敛后台线程：协作取消 → 强杀 → 父进程接管 canonical Trace 封存。

    CON-003：只有本地可控执行单元确认退出、Trace 完成 canonical run_cancelled 与封存后，
    业务对象才提交 cancelled；无法确认时保持 cancelling 或进入 cancel_timeout，不谎报。
    """
    from contracts.cancel_state import HARD_STOP_DEADLINE_SECONDS

    trace_id = worker.trace_id
    try:
        result = worker.stop_and_collect(deadline=HARD_STOP_DEADLINE_SECONDS)
        # 子进程若协作退出，自己已写 run_cancelled + 封存；若被强杀，则需父进程接管。
        converged = result.status
        _ab_tasks[task_id]["status"] = converged
        if result.trace_id:
            _ab_tasks[task_id]["trace_ids"] = [result.trace_id]
            trace_id = result.trace_id
        if result.error:
            _ab_tasks[task_id]["error"] = result.error

        # 父进程接管 canonical Trace 封存（FR-002/006 根因修复）。
        # seal_external_cancel 幂等：子进程已封存则不覆盖，强杀后无终态则补 run_cancelled + manifest。
        if trace_id:
            timeout = converged == "cancel_timeout"
            try:
                main_recorder.seal_external_cancel(
                    trace_id, cancel_audit=cancel_audit, timeout=timeout
                )
            except Exception:
                logger.exception("父进程接管 Trace 封存失败: task=%s trace=%s", task_id, trace_id)
                if not timeout:
                    _ab_tasks[task_id]["status"] = "cancel_timeout"
                    converged = "cancel_timeout"

        cancel_audit.converge_status = "cancelled" if converged == "cancelled" else "cancel_timeout"
        cancel_audit.converged_at = _now_iso()
        logger.info(
            "候选执行任务取消收敛完成: task=%s status=%s cancel_id=%s",
            task_id, converged, cancel_audit.cancel_id,
        )
    except Exception:
        logger.exception("取消收敛异常: task=%s", task_id)
        _ab_tasks[task_id]["status"] = "cancel_timeout"
        cancel_audit.converge_status = "cancel_timeout"
        cancel_audit.converged_at = _now_iso()
        # 异常路径仍尝试接管封存为 cancel_timeout（诚实告警，不谎报 cancelled）。
        if trace_id:
            try:
                main_recorder.seal_external_cancel(
                    trace_id, cancel_audit=cancel_audit, timeout=True
                )
            except Exception:
                logger.exception("异常路径接管封存失败: task=%s trace=%s", task_id, trace_id)


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


# 内存任务表（进程级。生产可换 Redis/DB）
_ab_tasks: dict[str, dict[str, Any]] = {}
