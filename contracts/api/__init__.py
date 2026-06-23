"""跨端 API 请求/响应模型 —— 执行端与进化端的共享契约。

执行端提供 /internal/* 端点，进化端是调用方。这里的模型定义了两端通信的「数据形状」，
作为单一真源，避免两端各定义一份导致不一致。

涉及的端点（均挂在 executor 的 /internal 路由下，设计文档 D3-dec / D7-dec）：
- GET  /internal/traces/{trace_id}     进化端拉取 trace 内容（D3 方案1）
- GET  /internal/traces?since=<ts>     进化端兜底拉取遗漏的 trace 列表（D3 scan 兜底）
- POST /internal/prompts/refreshed     进化端通知执行端「有新 prompt 版本」（D7 方案B）
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from contracts.trace import TraceLogEvent, TraceRunSummary


class TraceContentResponse(BaseModel):
    """GET /internal/traces/{trace_id} 的响应。

    进化端收到 trace 完成通知后，调此端点拉取完整 trace 内容（run 摘要 + 事件列表）。
    执行端从 jsonl 读取后按此格式返回。替代旧的「传文件路径让进化端读文件」的耦合方式。
    """

    run: TraceRunSummary
    events: list[TraceLogEvent] = Field(default_factory=list)


class TraceListItem(BaseModel):
    """trace 列表条目（GET /internal/traces 兜底扫描用）。

    进化端 scan 兜底时调列表端点，拿近期完成的 trace_id 清单，
    再逐个调 GET /internal/traces/{trace_id} 拉内容。
    """

    trace_id: str
    workspace_id: str
    status: str
    started_at: str
    ended_at: str | None = None


class TraceListResponse(BaseModel):
    """GET /internal/traces?since=<ts> 的响应。"""

    traces: list[TraceListItem] = Field(default_factory=list)


class PromptRefreshNotice(BaseModel):
    """POST /internal/prompts/refreshed 的请求体。

    进化端给某 prompt 版本打上 production label 后，发此通知给执行端。
    执行端收到后标记对应缓存为 stale，下次 load_prompt 时重新拉取。
    只带标识，不带内容——内容仍由执行端主动拉取（D7 方案B 设计）。
    """

    name: str
    version: int
    label: str = "production"
