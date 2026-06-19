"""image domain REST 路由（Phase 3.9，DD8b）。

端点：
- POST /api/image/generate/stream：文生图 SSE 流（复用 BaseAgentService 的 SSE 编排）
- GET /api/images/{image_id}：图片服务端点（DD8b，按 image_id 返回二进制）
- GET /api/skills：列出当前用户的所有 Skill（D18a）
- GET /api/skills/{skill_id}：读 Skill 正文（D18e 手动编辑前置）
- PUT /api/skills/{skill_id}：更新 Skill 元数据（D18b 重命名）
- DELETE /api/skills/{skill_id}：删除 Skill（D18d）
- POST /api/skills/merge：合并两份 Skill（D18c）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.auth import CurrentUser, current_user
from app.platform.agent.middleware import TraceCallbackHandler
from app.platform.core.db import SkillRepository, get_database
from app.platform.skills.loader import skills_root

router = APIRouter()

# image_service / thread_store / trace_recorder 由 main.py 注入（lifespan 后 set 到模块全局）
_image_service = None
_thread_store = None
_trace_recorder = None


def init_image_routes(image_service, thread_store, trace_recorder) -> None:
    """main.py 启动时注入 service 实例（PR-14 增加 trace_recorder）。"""
    global _image_service, _thread_store, _trace_recorder
    _image_service = image_service
    _thread_store = thread_store
    _trace_recorder = trace_recorder


# ── 文生图 SSE 流（PR-14 从 main.py 迁入）─────────────────

class ImageGenerateRequest(BaseModel):
    """文生图生成请求。"""
    thread_id: str
    prompt: str  # 用户想生成的图片描述
    trace_id: str | None = None
    resume: dict | None = None  # HITL resume（结构化 image_review 反馈，DD4）
    selected_skill_ids: list[str] | None = None  # D9 加载的私有 Skill


async def _image_event_generator(payload: ImageGenerateRequest, thread, *, owner_id: str):
    """文生图 SSE 流（platform.streaming.run_agent_stream 骨架）。

    image agent 走自己的 _build_agent，SSE 事件格式与写作一致（model_stream/
    tool_output/tool_error/interrupt/final），前端按 interrupt 的 kind 路由渲染。
    """
    from langgraph.types import Command
    from app.platform.streaming import run_agent_stream, sse as _sse
    trace_recorder = _trace_recorder
    model = _image_service._resolve_model(owner_id)
    checkpointer = await _image_service._resolve_checkpointer(owner_id)
    trace = trace_recorder.create_run(thread, "image.generate.stream")
    yield _sse("status", {"status": "started", "trace_id": trace.trace_id})

    agent = _image_service._build_agent(
        Path(thread.workspace_path), trace.trace_id, thread.workspace_id, owner_id,
        model=model, checkpointer=checkpointer,
        selected_skill_ids=payload.selected_skill_ids,
    )
    config = {
        "configurable": {"thread_id": thread.thread_id},
        "callbacks": [TraceCallbackHandler(trace_recorder, trace.trace_id)],
        "recursion_limit": 200,
    }
    if payload.resume is not None:
        agent_input = Command(resume=payload.resume)
    else:
        agent_input = {"messages": [{"role": "user", "content": payload.prompt}]}

    async def on_event(event: dict) -> list[str]:
        frames: list[str] = []
        kind = event["event"]
        data = event.get("data", {})
        if kind == "on_chat_model_stream":
            chunk = data.get("chunk")
            content = getattr(chunk, "content", "") if chunk else ""
            if content:
                frames.append(_sse("model_stream", {"content": content}))
        elif kind == "on_tool_end":
            frames.append(_sse("tool_output", {
                "tool": event.get("name", ""),
                "output": str(data.get("output", ""))[:2000],
            }))
        elif kind == "on_tool_error":
            frames.append(_sse("tool_error", {
                "tool": event.get("name", ""),
                "error": str(data.get("error") or data.get("output") or "")[:500],
            }))
        return frames

    class _ImageSink:
        async def on_event(self_inner, event: dict) -> list[str]:
            return await on_event(event)

    sse_iter, result = run_agent_stream(agent, agent_input, config, sink=_ImageSink())

    try:
        async for frame in sse_iter:
            yield frame

        if result.pending_interrupt is not None:
            iv = result.pending_interrupt
            payload_dict = iv if isinstance(iv, dict) else {"question": str(iv)}
            if "kind" not in payload_dict:
                payload_dict["kind"] = "choice"
            payload_dict["thread_id"] = thread.thread_id
            yield _sse("interrupt", payload_dict)
            return

        if result.full_result is not None:
            content = ""
            if isinstance(result.full_result, dict):
                for msg in reversed(result.full_result.get("messages", [])):
                    mc = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
                    if isinstance(mc, str) and mc:
                        content = mc
                        break
            yield _sse("final", {"content": content, "thread_id": thread.thread_id})
        trace_recorder.complete_run(thread, trace.trace_id)
    except BaseException as exc:
        trace_recorder.fail_run(thread, trace.trace_id, exc)
        yield _sse("error", {"error": str(exc)[:500]})
        raise


@router.post("/api/image/generate/stream")
async def stream_image(payload: ImageGenerateRequest, user: CurrentUser = Depends(current_user)):
    """文生图 SSE 流。

    interrupt 事件按 kind 路由：choice=访谈式 / image_review=图像评审。
    """
    thread = _thread_store.get_thread(user.user_id, payload.thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return StreamingResponse(
        _image_event_generator(payload, thread, owner_id=user.user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 图片服务端点（DD8b）───────────────────────────────────


@router.get("/api/images/{image_id}")
def serve_image(image_id: str, user: CurrentUser = Depends(current_user)) -> Response:
    """按 image_id 返回图片二进制（DD8b）。

    鉴权：current_user + owner_id 校验（只能访问自己的图）。
    """
    from app.platform.core.db import ImageRepository
    repo = ImageRepository(get_database())
    meta = repo.get(image_id, user.user_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Image not found")
    # 定位物理文件：workspace/<owner>/<ws_id>/<file_path>
    from app.platform.core.settings import get_settings
    settings = get_settings()
    ws_root = Path(settings.workspace_root)
    physical = ws_root / user.user_id / meta["workspace_id"] / meta["file_path"].lstrip("/")
    if not physical.exists():
        raise HTTPException(status_code=404, detail="Image file missing")
    ext = physical.suffix.lower().lstrip(".")
    content_type = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return Response(
        content=physical.read_bytes(),
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── Skill 管理（D18）──────────────────────────────────────


class SkillSummary(BaseModel):
    skill_id: str
    name: str
    scene_tag: str | None = None
    description: str = ""
    revision_count: int = 0
    created_at: str
    updated_at: str


class SkillUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=50)
    scene_tag: str | None = None
    description: str | None = None


class SkillMergeRequest(BaseModel):
    skill_id_1: str
    skill_id_2: str
    new_name: str = Field(min_length=1, max_length=50)
    new_content: str = Field(min_length=1, description="合并后的 SKILL.md 正文")
    new_scene_tag: str = ""


@router.get("/api/skills", response_model=list[SkillSummary])
def list_skills(user: CurrentUser = Depends(current_user)) -> list[SkillSummary]:
    """列出当前用户的所有 Skill（D18a）。"""
    repo = SkillRepository(get_database())
    return [SkillSummary(**sk) for sk in repo.list_by_owner(user.user_id)]


@router.get("/api/skills/{skill_id}")
def read_skill(skill_id: str, user: CurrentUser = Depends(current_user)) -> dict:
    """读 Skill 元数据 + 正文（D18e 手动编辑前置）。"""
    repo = SkillRepository(get_database())
    meta = repo.get(skill_id, user.user_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    skill_md = skills_root() / user.user_id / skill_id / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""
    return {**meta, "content": content}


@router.put("/api/skills/{skill_id}", response_model=SkillSummary)
def update_skill(
    skill_id: str, payload: SkillUpdateRequest,
    user: CurrentUser = Depends(current_user),
) -> SkillSummary:
    """更新 Skill 元数据（D18b 重命名/改标签）。"""
    repo = SkillRepository(get_database())
    updated = repo.update(
        skill_id, user.user_id,
        name=payload.name, scene_tag=payload.scene_tag, description=payload.description,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return SkillSummary(**updated)


@router.delete("/api/skills/{skill_id}")
def delete_skill(skill_id: str, user: CurrentUser = Depends(current_user)) -> dict:
    """删除 Skill（D18d：DB 删行 + 文件 rmtree）。"""
    import shutil
    repo = SkillRepository(get_database())
    if not repo.delete(skill_id, user.user_id):
        raise HTTPException(status_code=404, detail="Skill not found")
    skill_dir = skills_root() / user.user_id / skill_id
    if skill_dir.exists():
        shutil.rmtree(skill_dir, ignore_errors=True)
    return {"status": "ok", "deleted": skill_id}


@router.post("/api/skills/merge", response_model=SkillSummary)
def merge_skills(
    payload: SkillMergeRequest,
    user: CurrentUser = Depends(current_user),
) -> SkillSummary:
    """合并两份 Skill（D18c）。

    合并 = 新建 Skill（用 new_content）+ 删除原两份。
    new_content 由前端/用户准备好（语义融合后的正文）。
    """
    repo = SkillRepository(get_database())
    # 校验两份都存在且属于当前用户
    for sid in (payload.skill_id_1, payload.skill_id_2):
        if repo.get(sid, user.user_id) is None:
            raise HTTPException(status_code=404, detail=f"Skill not found: {sid}")
    # 新建合并后的 Skill
    new_meta = repo.create(
        owner_id=user.user_id, name=payload.new_name, scene_tag=payload.new_scene_tag or None,
    )
    new_dir = skills_root() / user.user_id / new_meta["skill_id"]
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / "SKILL.md").write_text(payload.new_content, encoding="utf-8")
    # 删除原两份（DB + 文件）
    import shutil
    for sid in (payload.skill_id_1, payload.skill_id_2):
        repo.delete(sid, user.user_id)
        old_dir = skills_root() / user.user_id / sid
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)
    return SkillSummary(**new_meta)


__all__ = ["router", "init_image_routes"]
