"""character 生成路由（PR-14 从 main.py 抽出）。

POST /api/character/generate —— 角色生成（同步）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth import CurrentUser, current_user
from app.routers.context import get_character_service, get_thread_store
from app.schemas.character import CharacterGenerateRequest, CharacterGenerateResponse

router = APIRouter()


@router.post("/character/generate", response_model=CharacterGenerateResponse)
def generate_character(payload: CharacterGenerateRequest, user: CurrentUser = Depends(current_user)) -> CharacterGenerateResponse:
    thread = get_thread_store().get_thread(user.user_id, payload.thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    response = get_character_service().generate(payload, thread, owner_id=user.user_id)
    get_thread_store().artifacts.write_character(user.user_id, thread, response)
    return response
