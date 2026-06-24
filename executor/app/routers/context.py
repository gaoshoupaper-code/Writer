"""app/routers/context.py —— 路由层共享的 service 实例 + 诊断工具（PR-14）。

main.py 在 lifespan 启动时通过 init_router_context() 注入 service 实例，
各 domain router 通过 get_*() 访问。沿用 image_router 的注入模式但集中管理。

同时提供横切诊断工具（_log / active_generations 计数），供 SSE 生成端点共享。
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.platform.state.thread_store import ThreadStore
    from app.domains.writing.styling.store import CreateTypeStore
    from app.platform.trace import TraceRecorder
    from app.domains.writing.agent import MetaAgentService
    from app.domains.writing.expert_agent.services.character import CharacterService
    from app.domains.image.agent import ImageAgentService

# 模块级单例（main.py 注入前为 None）
_thread_store: "ThreadStore | None" = None
_style_store: "CreateTypeStore | None" = None
_trace_recorder: "TraceRecorder | None" = None
_agent_service: "MetaAgentService | None" = None
_character_service: "CharacterService | None" = None
_image_agent_service: "ImageAgentService | None" = None
_style_optimizer = None

# 横切诊断：SSE 活跃连接计数（screenplay + image 共享）
_active_generations = 0


def _log(event: str, **fields) -> None:
    """诊断日志：结构化 JSON 到 stdout（flush 保证即时输出）。

    为 Phase 1 根因诊断服务——定位 SSE 连接泄漏 / 请求挂起 / 锁竞争。
    """
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    print(json.dumps(record, ensure_ascii=False), flush=True)


def generation_started() -> int:
    """SSE 生成开始，返回当前活跃数（用于日志）。线程安全由 asyncio 单线程保证。"""
    global _active_generations
    _active_generations += 1
    return _active_generations


def generation_finished() -> int:
    """SSE 生成结束，返回剩余活跃数。"""
    global _active_generations
    _active_generations -= 1
    return _active_generations


def init_router_context(
    *,
    thread_store: "ThreadStore",
    style_store: "CreateTypeStore",
    trace_recorder: "TraceRecorder",
    agent_service: "MetaAgentService",
    character_service: "CharacterService",
    image_agent_service: "ImageAgentService",
    style_optimizer,
) -> None:
    """main.py 启动时注入全部 service 实例。"""
    global _thread_store, _style_store, _trace_recorder
    global _agent_service, _character_service, _image_agent_service, _style_optimizer
    _thread_store = thread_store
    _style_store = style_store
    _trace_recorder = trace_recorder
    _agent_service = agent_service
    _character_service = character_service
    _image_agent_service = image_agent_service
    _style_optimizer = style_optimizer


def get_thread_store() -> "ThreadStore":
    assert _thread_store is not None, "router context 未初始化（main.py lifespan 未注入）"
    return _thread_store


def get_style_store() -> "CreateTypeStore":
    assert _style_store is not None, "router context 未初始化"
    return _style_store


def get_trace_recorder() -> "TraceRecorder":
    assert _trace_recorder is not None, "router context 未初始化"
    return _trace_recorder


def get_agent_service() -> "MetaAgentService":
    assert _agent_service is not None, "router context 未初始化"
    return _agent_service


def get_character_service() -> "CharacterService":
    assert _character_service is not None, "router context 未初始化"
    return _character_service


def get_image_agent_service() -> "ImageAgentService":
    assert _image_agent_service is not None, "router context 未初始化"
    return _image_agent_service


def get_style_optimizer():
    assert _style_optimizer is not None, "router context 未初始化"
    return _style_optimizer
