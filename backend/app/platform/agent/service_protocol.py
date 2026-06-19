"""platform.agent.service_protocol —— AgentService 协议（PR-08）。

定义所有 domain service（MetaAgentService / ImageAgentService /
CharacterService）的统一契约。BaseAgentService 实现本协议；
各 domain service 继承 BaseAgentService 获得协议一致性。

协议聚焦 checkpoint 生命周期管理（多用户隔离的硬红线相关）：
- get_thread_checkpoint：读取 thread 的最新 checkpoint
- delete_thread_checkpoint：删除 thread 的 checkpoint

generate_stream 的签名因 domain 而异（writing 用 ScreenplayGenerateRequest，
image 用 ImageGenerateRequest），不强制纳入本协议——各 service 自行定义，
但都产出 SSE 流（platform.streaming.run_agent_stream 骨架统一）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.checkpoint import CheckpointState


@runtime_checkable
class AgentService(Protocol):
    """domain agent 服务的统一契约。

    所有 agent service（写作/文生图/角色生成）必须实现本协议。
    通过继承 BaseAgentService 自动获得实现；CharacterService 将在 PR-12 补齐继承。
    """

    async def get_thread_checkpoint(
        self, thread_id: str, *, owner_id: str | None = None,
    ) -> CheckpointState:
        """读取 thread 的最新 checkpoint，规范化为 CheckpointState。"""
        ...

    async def delete_thread_checkpoint(
        self, thread_id: str, *, owner_id: str | None = None,
    ) -> None:
        """删除 thread 的 checkpoint（多用户隔离：按 owner 清理分库 saver）。

        async：分库 saver 是异步惰性创建，必须用 async 接口才能取到 owner 对应的
        per-user .db 并真正清理（PR-10 修复：原同步实现删不到分库 checkpoint）。
        """
        ...


__all__ = ["AgentService"]
