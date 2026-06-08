from pydantic import BaseModel


class CheckpointToolCall(BaseModel):
    name: str
    id: str


class CheckpointMessage(BaseModel):
    role: str  # "system" | "human" | "ai" | "tool"
    content: str
    tool_calls: list[CheckpointToolCall] | None = None
    name: str | None = None


class CheckpointState(BaseModel):
    thread_id: str
    messages: list[CheckpointMessage]
