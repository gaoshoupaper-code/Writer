"""screenplay 生成路由（PR-14 从 main.py 抽出）。

POST /api/screenplay/generate/stream —— 写作 SSE 流。
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.auth import CurrentUser, current_user
from app.routers.context import (
    _log,
    generation_finished,
    generation_started,
    get_agent_service,
    get_thread_store,
    get_trace_recorder,
)
from pydantic import BaseModel
from app.schemas.screenplay import (
    ScreenplayGenerateRequest,
    ScreenplayGenerateResponse,
    ThreadSummary,
)

router = APIRouter()


async def _event_generator(payload: ScreenplayGenerateRequest, thread: ThreadSummary, *, owner_id: str | None = None):
    """Async generator that yields SSE events from the agent execution."""
    _log("sse_open", channel="generate", thread_id=payload.thread_id, active=generation_started())
    start = time.perf_counter()
    final_data = None
    agent_service = get_agent_service()
    thread_store = get_thread_store()
    try:
        async for chunk in agent_service.generate_stream(payload, thread, owner_id=owner_id):
            yield chunk
            if chunk.startswith("event: final"):
                for line in chunk.split("\n"):
                    if line.startswith("data: "):
                        final_data = line[6:]
                        break
        if final_data:
            response = ScreenplayGenerateResponse.model_validate(json.loads(final_data))
            thread_store.artifacts.write_outline(owner_id or "", thread, response)
        _log("sse_close", channel="generate", thread_id=payload.thread_id,
             ms=int((time.perf_counter() - start) * 1000))
    except BaseException as exc:
        _log("sse_error", channel="generate", thread_id=payload.thread_id,
             error=type(exc).__name__, ms=int((time.perf_counter() - start) * 1000))
        raise
    finally:
        _log("sse_exit", channel="generate", thread_id=payload.thread_id, active=generation_finished())


@router.post("/screenplay/generate/stream")
async def stream_screenplay(
    payload: ScreenplayGenerateRequest,
    user: CurrentUser = Depends(current_user),
):
    # AD12：冻结拦截（D16/D26）——余额≤0 不可触发创作
    from app.platform.credits.dependencies import check_credits_frozen
    check_credits_frozen(user)

    thread = get_thread_store().get_thread(user.user_id, payload.thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return StreamingResponse(
        _event_generator(payload, thread, owner_id=user.user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class StopRequest(BaseModel):
    """停止生成请求（D6 停止信号：前端"停止"按钮显式发送）。"""

    thread_id: str
    trace_id: str


@router.post("/screenplay/stop")
def stop_screenplay(req: StopRequest, user: CurrentUser = Depends(current_user)):
    """标记用户请求停止某条 trace（D6 + D-停止真生效）。

    两步：
    1. request_user_stop 打 _user_stop_requested 标记——决定 cancel reason 文案
       （user_stop vs client_disconnect），原有逻辑保留。
    2. cancel_run_task 真正 task.cancel()——把 CancelledError 注入生成器，
       走 generate_stream 的 except 三路分流收尾成 cancelled。

    第 2 步是关键补丁：原来"真停止"完全依赖前端 abort SSE 连接，浏览器刷新/关闭/
    cloudflared 掐断后 abortController 丢失，后台 trace 就停不掉。现在后端主动 kill，
    不依赖前端连接状态。幂等：trace 已结束/未登记返回 task_cancelled=False，不抛错。
    """
    recorder = get_trace_recorder()
    recorder.request_user_stop(req.trace_id)
    cancelled = recorder.cancel_run_task(req.trace_id)
    return {"status": "accepted", "trace_id": req.trace_id, "task_cancelled": cancelled}


# ── 数据闭环 E2：隐式反馈信号埋点 ──────────────────────────


class SignalRequest(BaseModel):
    """用户行为信号埋点请求（数据闭环 E2，copy/regenerate）。

    trace_id 标识哪次生成；前端 fire-and-forget 调用，失败不影响用户操作。
    """
    trace_id: str
    content_preview: str = ""   # 复制的内容片段（前 200 字符，供 debug）


@router.post("/screenplay/copy")
def track_copy(req: SignalRequest, user: CurrentUser = Depends(current_user)):
    """记录"用户复制了内容"信号（正信号，数据闭环 E2/D15）。

    前端内容面板的"复制"按钮调此端点。recorder 写 user_copy 事件进 trace jsonl，
    进化端 promote 闸门据此判质量（copy = 用户认可）。
    """
    _append_user_signal(req.trace_id, "user_copy", {
        "user_id": user.user_id,
        "content_preview": req.content_preview[:200],
    })
    return {"status": "accepted"}


@router.post("/screenplay/regenerate")
def track_regenerate(req: SignalRequest, user: CurrentUser = Depends(current_user)):
    """记录"用户点了重试/重新生成"信号（负信号，数据闭环 E2/D15）。

    前端"重试"按钮调此端点。recorder 写 user_regenerate 事件进 trace jsonl，
    进化端 promote 闸门据此判质量（regenerate = 用户不满意）。
    """
    _append_user_signal(req.trace_id, "user_regenerate", {
        "user_id": user.user_id,
        "content_preview": req.content_preview[:200],
    })
    return {"status": "accepted"}


def _append_user_signal(trace_id: str, event_type: str, payload: dict) -> None:
    """写一条用户行为信号事件到 trace jsonl（容错：trace 不存在/已清理则静默跳过）。

    用户复制/重试通常发生在生成完成后，此时 trace sequence 可能已清理。
    append_event 失败不阻断用户操作（fire-and-forget）。
    """
    recorder = get_trace_recorder()
    try:
        recorder.append_event(trace_id, {
            "type": event_type,
            "status": "completed",
            "source": "system",
            "output": payload,
        })
    except (KeyError, Exception) as exc:
        # trace 已结束清理（_sequences 无此 trace_id）或 recorder 未就绪
        _log("signal_drop", trace_id=trace_id, event_type=event_type,
             error=str(exc)[:200])
