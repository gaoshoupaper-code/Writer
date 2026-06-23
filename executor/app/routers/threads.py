"""threads 路由（PR-14 从 main.py 抽出）。

线程 CRUD + outline + checkpoint + trace 查询/删除。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth import CurrentUser, current_user
from app.routers.context import (
    get_agent_service,
    get_character_service,
    get_thread_store,
    get_trace_recorder,
)
from app.schemas.checkpoint import CheckpointState
from app.schemas.screenplay import (
    ThreadCreateRequest,
    ThreadSummary,
    ThreadUpdateRequest,
    WorkspaceOutlineContent,
)
from app.platform.trace import TraceDetail, TraceRunSummary

router = APIRouter()


@router.get("/threads", response_model=list[ThreadSummary])
def list_threads(workspace_id: str | None = None, user: CurrentUser = Depends(current_user)) -> list[ThreadSummary]:
    return get_thread_store().list_threads(user.user_id, workspace_id)


@router.post("/threads", response_model=ThreadSummary)
def create_thread(payload: ThreadCreateRequest, user: CurrentUser = Depends(current_user)) -> ThreadSummary:
    thread_store = get_thread_store()
    try:
        return thread_store.create_thread(user.user_id, payload.workspace_id, payload.session_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/threads/{thread_id}", response_model=ThreadSummary)
def update_thread(thread_id: str, payload: ThreadUpdateRequest, user: CurrentUser = Depends(current_user)) -> ThreadSummary:
    thread_store = get_thread_store()
    try:
        thread = thread_store.update_thread_name(user.user_id, thread_id, payload.session_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread


@router.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, str | bool]:
    thread_store = get_thread_store()
    trace_recorder = get_trace_recorder()
    thread = thread_store.get_thread(user.user_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    try:
        trace_recorder.delete_thread_runs(thread)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    deleted = thread_store.delete_thread(user.user_id, thread_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found")
    await get_agent_service().delete_thread_checkpoint(thread_id, owner_id=user.user_id)
    await get_character_service().delete_thread_checkpoint(thread_id)
    return {"status": "ok", "deleted": thread_id}


@router.get("/threads/{thread_id}/outline", response_model=WorkspaceOutlineContent)
def get_thread_outline(thread_id: str, user: CurrentUser = Depends(current_user)) -> WorkspaceOutlineContent:
    content = get_thread_store().artifacts.read_thread_outline(user.user_id, thread_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return content


@router.get("/threads/{thread_id}/checkpoint", response_model=CheckpointState)
async def get_thread_checkpoint(thread_id: str, user: CurrentUser = Depends(current_user)) -> CheckpointState:
    thread_store = get_thread_store()
    thread = thread_store.get_thread(user.user_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return await get_agent_service().get_thread_checkpoint(thread_id, owner_id=user.user_id)


@router.get("/threads/{thread_id}/traces", response_model=list[TraceRunSummary])
def list_thread_traces(thread_id: str, user: CurrentUser = Depends(current_user)) -> list[TraceRunSummary]:
    thread_store = get_thread_store()
    trace_recorder = get_trace_recorder()
    thread = thread_store.get_thread(user.user_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return trace_recorder.list_runs(thread)


@router.get("/threads/{thread_id}/traces/{trace_id}", response_model=TraceDetail)
def get_thread_trace(thread_id: str, trace_id: str, user: CurrentUser = Depends(current_user)) -> TraceDetail:
    thread_store = get_thread_store()
    trace_recorder = get_trace_recorder()
    thread = thread_store.get_thread(user.user_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    try:
        detail = trace_recorder.read_run(thread, trace_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return detail


@router.delete("/threads/{thread_id}/traces/{trace_id}")
def delete_thread_trace(thread_id: str, trace_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, str]:
    thread_store = get_thread_store()
    trace_recorder = get_trace_recorder()
    thread = thread_store.get_thread(user.user_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    try:
        deleted = trace_recorder.delete_run(thread, trace_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"status": "ok", "deleted": trace_id}
