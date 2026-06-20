"""internal 诊断路由（Phase 6 T15 + Phase 3 T3.1）。

供 monitoring：
  - GET  /internal/active-runs：轮询拉取活跃 trace 列表（活跃大盘）
  - POST /internal/ab-replay：A/B 回放——用指定 prompt label 跑一次生成
    （trace 标 run_purpose=optimization，监测层断路不进优化池）

内部接口，无鉴权（monitoring 与后端同信任域），不暴露给终端用户。

设计依据：T15（活跃大盘）+ T3.1（A/B 回放端点，D5 复用生成链路）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.routers.context import get_agent_service, get_thread_store, get_trace_recorder

logger = logging.getLogger("writer.internal")

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)

# A/B 回放专用系统账号（不污染用户数据，trace 走独立 workspace）
AB_REPLAY_OWNER = "ab-replay"


@router.get("/active-runs")
def active_runs() -> list[dict[str, Any]]:
    """当前活跃 trace 列表（T15 活跃大盘）。

    monitoring 定期轮询此端点，展示"哪些 trace 在跑、跑了多久"。
    纯内存读取，不涉及文件 IO。
    """
    return get_trace_recorder().list_active_runs()


# ── Phase 3 T3.1：A/B 回放端点 ──────────────────────────────


class ABReplayRequest(BaseModel):
    """A/B 回放请求（monitoring 的 experiment.py 调用）。"""

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

    trace 标 run_purpose=optimization → monitoring 摄入但断路不进优化池（防自指）。
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
