"""BaseAgentService — 领域无关的 agent 服务基类（DD7c）。

提取自 MetaAgentService 的通用部分：多用户 model/checkpointer 解析、
workspace backend 构建、checkpoint 读写。各 domain（writing/image/...）的
service 继承此类，通过钩子注入领域差异。

设计（DD7c 模板方法）：
- 基类提供基础设施（model/checkpointer 解析、backend 构建、checkpoint 读写）。
- ``generate_stream`` 的 SSE 编排骨架计划收敛到基类（后续逐步提取，保证写作零回归）。
- 子类负责：``_build_agent`` / ``_build_user_prompt`` / ``_extract_response`` /
  ``_domain_middleware`` 等领域钩子。

Phase 2 阶段策略：
- 本基类先落地基础设施层（model/checkpointer/backend/checkpoint 读写）。
- MetaAgentService 继承 BaseAgentService，复用这些方法，行为不变。
- generate_stream 的完整模板方法提取留待后续（避免一次性重构 200+ 行 SSE 编排
  引入回归风险）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.platform.core.settings import Settings
from app.platform.agent.runtime import FilesystemBackend
from app.schemas.checkpoint import CheckpointMessage, CheckpointState, CheckpointToolCall


class BaseAgentService:
    """领域无关的 agent 服务基类。

    实现 ``AgentService`` 协议（get_thread_checkpoint / delete_thread_checkpoint）。
    提供 model/checkpointer 解析、workspace backend 构建、checkpoint 读写等
    通用能力。各 domain service 继承此类。
    """

    def __init__(
        self,
        settings: Settings,
        workspace_root: Path,
        trace_recorder: Any,
        checkpointer: BaseCheckpointSaver,
    ) -> None:
        self.settings = settings
        self.workspace_root = workspace_root
        self.trace_recorder = trace_recorder
        self.checkpointer = checkpointer

    # ── workspace backend ────────────────────────────────────

    def _backend_for_workspace(self, workspace_path: Path) -> FilesystemBackend:
        """构建 workspace 的虚拟文件系统 backend。"""
        workspace_path.mkdir(parents=True, exist_ok=True)
        return FilesystemBackend(root_dir=workspace_path, virtual_mode=True)

    # ── 多用户隔离：model / checkpointer 解析 ────────────────

    def _resolve_model(self, owner_id: str | None):
        """按 owner 解密其 API key 构建 model；无 owner 或无 key 回退全局 settings。

        子类可通过覆盖 ``_build_model_with_key`` 钩子定制 model 构建（如 image
        domain 用视觉模型）。默认走写作的 build_writer_model。
        """
        if not owner_id:
            return self._build_model_default()
        try:
            from app.db import get_database, UserRepository
            users = UserRepository(get_database())
            key, base_url, model = users.get_api_key_plain(owner_id)
            if key is None:
                # 用户未填 key：回退全局（管理员兜底）
                return self._build_model_default()
            return self._build_model_with_key(key, base_url, model)
        except Exception:
            return self._build_model_default()

    def _build_model_default(self):
        """无 owner/key 时构建 model。子类必须覆盖（提供各自领域的默认模型）。

        PR-12 修复 R1：原默认实现硬编码 build_writer_model（domains.writing），
        导致 platform→domains 反向依赖。现改为抽象，由子类注入领域模型。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 必须覆盖 _build_model_default（提供领域默认模型）"
        )

    def _build_model_with_key(self, key: str, base_url: str | None, model_name: str | None):
        """按 owner 的 key 构建 model。子类必须覆盖（提供各自领域的模型构建）。"""
        raise NotImplementedError(
            f"{type(self).__name__} 必须覆盖 _build_model_with_key（提供领域模型构建）"
        )

    async def _resolve_checkpointer(self, owner_id: str | None):
        """按 owner 取分库 saver；无 owner 回退全局（兼容/测试）。"""
        if not owner_id:
            return self.checkpointer
        try:
            from app.platform.core.checkpoint_pool import get_checkpoint_pool
            return await get_checkpoint_pool().get(owner_id)
        except Exception:
            return self.checkpointer

    def _resolve_checkpointer_sync(self, owner_id: str | None):
        """同步路径的 checkpointer（只能用全局兜底，分库 saver 是异步惰性创建）。

        仅用于无法 await 的同步上下文。分库数据的可靠删除请用 delete_thread_checkpoint
        （async，PR-10 修复：原来同步调全局 saver 删不到分库 checkpoint）。
        """
        return self.checkpointer

    # ── checkpoint 读写 ──────────────────────────────────────

    async def delete_thread_checkpoint(self, thread_id: str, *, owner_id: str | None = None) -> None:
        """删除 thread 的 checkpoint（PR-10 修复：改为 async，用分库 saver）。

        原同步实现调 _resolve_checkpointer_sync 返回全局 saver，分库（per-user .db）
        数据不在全局库，删除是空操作——导致 delete_thread 后 checkpoint 残留。
        现改为 async，走 _resolve_checkpointer 取到 owner 对应的分库 saver，真正清理。
        """
        checkpointer = await self._resolve_checkpointer(owner_id)
        await checkpointer.adelete_thread(thread_id)

    async def get_thread_checkpoint(self, thread_id: str, *, owner_id: str | None = None) -> CheckpointState:
        """读取 thread 的最新 checkpoint，规范化为 CheckpointState。"""
        checkpointer = await self._resolve_checkpointer(owner_id)
        config = {"configurable": {"thread_id": thread_id}}
        checkpoint = await checkpointer.aget(config)
        if checkpoint is None:
            return CheckpointState(thread_id=thread_id, messages=[])
        channel_values = checkpoint.get("channel_values", {})
        raw_messages = channel_values.get("messages", [])
        messages = []
        for msg in raw_messages:
            try:
                messages.append(_normalize_message(msg))
            except Exception:
                continue
        return CheckpointState(thread_id=thread_id, messages=messages)


# ======================================================================
# 消息规范化（从 meta/agent.py 提取，领域无关）
# ======================================================================


def _normalize_message(msg: object) -> CheckpointMessage:
    """将 LangChain BaseMessage 转为 CheckpointMessage schema。"""
    if isinstance(msg, dict):
        role = str(msg.get("type", msg.get("role", ""))).lower()
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
                if (isinstance(block, dict) and block.get("type") == "text") or isinstance(block, str)
            )
        content = str(content) if content else ""
        tool_calls = None
        raw_calls = msg.get("tool_calls")
        if isinstance(raw_calls, list):
            tool_calls = [
                CheckpointToolCall(name=str(tc.get("name", "")), id=str(tc.get("id", "")))
                for tc in raw_calls
                if isinstance(tc, dict)
            ]
        name = msg.get("name")
        return CheckpointMessage(
            role=_map_role(role),
            content=content,
            tool_calls=tool_calls,
            name=str(name) if name else None,
        )

    msg_type = getattr(msg, "type", "") or ""
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        content = "\n".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if (isinstance(block, dict) and block.get("type") == "text") or isinstance(block, str)
        )
    content = str(content) if content else ""

    tool_calls = None
    raw_calls = getattr(msg, "tool_calls", None)
    if isinstance(raw_calls, list):
        tool_calls = [
            CheckpointToolCall(name=str(tc.get("name", "")), id=str(tc.get("id", "")))
            for tc in raw_calls
            if isinstance(tc, dict)
        ]

    name = getattr(msg, "name", None)
    return CheckpointMessage(
        role=_map_role(msg_type),
        content=content,
        tool_calls=tool_calls,
        name=str(name) if name else None,
    )


def _map_role(msg_type: str) -> str:
    """将 LangChain 消息类型映射为标准化 role。"""
    mapping = {
        "system": "system",
        "human": "human",
        "user": "human",
        "ai": "ai",
        "assistant": "ai",
        "tool": "tool",
    }
    return mapping.get(msg_type, msg_type)


__all__ = ["BaseAgentService"]
