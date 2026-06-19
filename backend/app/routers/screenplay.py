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
)
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
async def stream_screenplay(payload: ScreenplayGenerateRequest, user: CurrentUser = Depends(current_user)):
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
