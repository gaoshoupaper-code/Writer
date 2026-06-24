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
from typing import Any

from fastapi import APIRouter, HTTPException, Query
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
def get_trace_content(trace_id: str) -> TraceContentResponse:
    """拉取 trace 完整内容（run 摘要 + 事件列表）。

    evolution 收到 trace 完成通知后调此端点，替代旧的「传文件路径让 evolution
    读文件」的耦合方式。依赖 recorder 的 _trace_workspace 索引定位 workspace。

    404 场景：trace_id 不在索引中（进程重启导致索引丢失），evolution 应靠
    scan 兜底（GET /internal/traces）补拉。
    """
    recorder = get_trace_recorder()
    run = recorder.find_run_by_trace_id(trace_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"trace_id not found: {trace_id}")
    events = recorder.read_trace_events(trace_id)
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
