from typing import Any

from pydantic import BaseModel, ConfigDict


class CharacterGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str | None = None
    content: str | None = None
    text: str | None = None
    name: str | None = None
    role: str | None = None
    description: str | None = None
    thread_id: str

    def primary_text(self) -> str:
        return self.prompt or self.content or self.text or self.description or ""

    def fallback_name(self) -> str:
        return self.name or "未命名角色"

    def loose_context(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, exclude={"thread_id"})


class CharacterGenerateResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    mode: str
    thread_id: str
    workspace_id: str
    session_name: str
    workspace_path: str
    name: str = "未命名角色"
    identity: str = ""
    appearance: str = ""
    personality: str = ""
    current_state: str = ""
    relationships: str = ""
    markdown: str = ""
