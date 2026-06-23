from pydantic import BaseModel, Field


class StyleCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class StyleSummary(BaseModel):
    style_id: str
    name: str
    description: str
    created_at: str


class WorkspaceStyleRequest(BaseModel):
    style_id: str | None = None
