from fastapi import APIRouter, HTTPException

from app.create_type.optimizer import StyleOptimizer, VALID_STYLE_TYPES
from app.create_type.schemas import (
    StyleCreateRequest,
    StyleOptimizeRequest,
    StyleOptimizeResponse,
    StyleSummary,
    StyleUpdateRequest,
    WorkspaceStyleRequest,
)
from app.create_type.store import CreateTypeStore
from app.schemas.screenplay import WorkspaceSummary

router = APIRouter(prefix="/api", tags=["styles"])

# These will be set by main.py during app initialization
_store: CreateTypeStore | None = None
_optimizer: StyleOptimizer | None = None


def init_style_module(store: CreateTypeStore, optimizer: StyleOptimizer) -> None:
    global _store, _optimizer
    _store = store
    _optimizer = optimizer


def _require_store() -> CreateTypeStore:
    if _store is None:
        raise RuntimeError("Style module not initialized")
    return _store


def _require_optimizer() -> StyleOptimizer:
    if _optimizer is None:
        raise RuntimeError("Style optimizer not initialized")
    return _optimizer


@router.get("/styles", response_model=list[StyleSummary])
def list_styles() -> list[StyleSummary]:
    return _require_store().list_styles()


@router.post("/styles", response_model=StyleSummary)
def create_style(payload: StyleCreateRequest) -> StyleSummary:
    return _require_store().create_style(
        name=payload.name,
        meta_style=payload.meta_style,
        character_style=payload.character_style,
        outline_style=payload.outline_style,
        detail_outline_style=payload.detail_outline_style,
        writing_style=payload.writing_style,
    )


@router.put("/styles/{style_id}", response_model=StyleSummary)
def update_style(style_id: str, payload: StyleUpdateRequest) -> StyleSummary:
    store = _require_store()
    result = store.update_style(
        style_id,
        name=payload.name,
        meta_style=payload.meta_style,
        character_style=payload.character_style,
        outline_style=payload.outline_style,
        detail_outline_style=payload.detail_outline_style,
        writing_style=payload.writing_style,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Style not found")
    return result


@router.delete("/styles/{style_id}")
def delete_style(style_id: str) -> dict[str, str | bool]:
    deleted = _require_store().delete_style(style_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Style not found")
    return {"status": "ok", "deleted": style_id}


@router.put("/workspaces/{workspace_id}/style", response_model=WorkspaceSummary)
def set_workspace_style(workspace_id: str, payload: WorkspaceStyleRequest) -> WorkspaceSummary:
    store = _require_store()
    from app.core.thread_store import ThreadStore

    workspace = store.thread_store.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        store.set_active_style_id(workspace_id, payload.style_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return store.thread_store.get_workspace(workspace_id)


@router.post("/styles/optimize", response_model=StyleOptimizeResponse)
async def optimize_style(payload: StyleOptimizeRequest) -> StyleOptimizeResponse:
    if payload.style_type not in VALID_STYLE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid style_type. Must be one of: {', '.join(sorted(VALID_STYLE_TYPES))}",
        )
    try:
        optimized = await _require_optimizer().optimize(payload.style_type, payload.content)
        return StyleOptimizeResponse(optimized=optimized)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Optimization failed: {exc}") from exc
