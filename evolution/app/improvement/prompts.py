"""prompt 版本管理 API 路由（Phase 4 T9/T10）。

两类端点：
- /api/prompts/* : 管理用（CRUD、版本、label），供管理页面
- /api/prompts/{name} GET : 拉取用（后端 loader 按 label 拉 production）

设计依据：T9（统一 loader）、T10（evolution 主 + 后端从）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import app.improvement.prompts_repo as repo

router = APIRouter(tags=["prompts"])


# ── 拉取端点（后端 loader 用）──


@router.get("/prompts/{name}")
def get_prompt(
    name: str,
    label: str = Query(repo.PRODUCTION_LABEL, description="按 label 拉取，默认 production"),
) -> dict[str, Any]:
    """按 name + label 拉取 prompt 内容（后端 loader 主入口 T10）。

    label 未找到时回退到最新版本（max version），保证 loader 总能拿到内容。
    """
    result = repo.get_prompt_content(name, label)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Prompt not found: {name}")
    return result


# ── 管理端点（CRUD）──


class CreatePromptBody(BaseModel):
    name: str
    type: str = "text"


class CreateVersionBody(BaseModel):
    content: str
    commit_message: str | None = None
    source: str = "manual"
    config: dict[str, Any] | None = None
    labels: list[str] | None = None


class SetLabelsBody(BaseModel):
    labels: list[str]


@router.get("/prompts")
def list_prompts() -> list[dict[str, Any]]:
    return repo.list_prompts()


@router.post("/prompts")
def create_prompt(body: CreatePromptBody) -> dict[str, Any]:
    try:
        return repo.create_prompt(body.name, body.type)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/prompts/{name}/versions")
def list_versions(name: str) -> list[dict[str, Any]]:
    prompt = repo.get_prompt_by_name(name)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt not found: {name}")
    return repo.list_versions(prompt["id"])


@router.post("/prompts/{name}/versions")
def create_version(name: str, body: CreateVersionBody) -> dict[str, Any]:
    prompt = repo.get_prompt_by_name(name)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt not found: {name}")
    return repo.create_version(
        prompt["id"], body.content, body.commit_message, body.source, body.config, body.labels
    )


@router.patch("/prompt-versions/{version_id}/labels")
def set_labels(version_id: int, body: SetLabelsBody) -> dict[str, str]:
    try:
        repo.set_labels(version_id, body.labels)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


@router.delete("/prompts/{name}")
def delete_prompt(name: str) -> dict[str, str]:
    prompt = repo.get_prompt_by_name(name)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt not found: {name}")
    repo.delete_prompt(prompt["id"])
    return {"status": "ok"}
