from pydantic import BaseModel, Field


class StyleCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    meta_style: str = Field(default="")
    character_style: str = Field(default="")
    outline_style: str = Field(default="")
    detail_outline_style: str = Field(default="")
    writing_style: str = Field(default="")


class StyleUpdateRequest(BaseModel):
    name: str | None = None
    meta_style: str | None = None
    character_style: str | None = None
    outline_style: str | None = None
    detail_outline_style: str | None = None
    writing_style: str | None = None


class StyleSummary(BaseModel):
    style_id: str
    name: str
    meta_style: str
    character_style: str
    outline_style: str
    detail_outline_style: str
    writing_style: str
    created_at: str


class WorkspaceStyleRequest(BaseModel):
    style_id: str | None = None


class StyleOptimizeRequest(BaseModel):
    style_type: str = Field(description="meta_style | character_style | outline_style | detail_outline_style | writing_style")
    content: str = Field(min_length=1)


class StyleOptimizeResponse(BaseModel):
    optimized: str
