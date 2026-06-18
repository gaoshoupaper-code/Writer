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
from app.db import SkillRepository, get_database
from app.platform.skills.loader import skills_root

router = APIRouter()

# image_service 和 thread_store 由 main.py 注入（lifespan 后 set 到模块全局）
_image_service = None
_thread_store = None


def init_image_routes(image_service, thread_store) -> None:
    """main.py 启动时注入 service 实例。"""
    global _image_service, _thread_store
    _image_service = image_service
    _thread_store = thread_store


# ── 图片服务端点（DD8b）───────────────────────────────────


@router.get("/api/images/{image_id}")
def serve_image(image_id: str, user: CurrentUser = Depends(current_user)) -> Response:
    """按 image_id 返回图片二进制（DD8b）。

    鉴权：current_user + owner_id 校验（只能访问自己的图）。
    """
    from app.db import ImageRepository
    repo = ImageRepository(get_database())
    meta = repo.get(image_id, user.user_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Image not found")
    # 定位物理文件：workspace/<owner>/<ws_id>/<file_path>
    from app.core.settings import get_settings
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
