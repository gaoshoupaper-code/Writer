"""platform.streaming —— SSE 流式编排统一骨架（PR-07a）。

消灭三份重复的 SSE 编排，提供 run_agent_stream 共同骨架 + sse 工具函数。
各 domain（writing/image）通过实现 EventSink 注入领域专属的事件分发逻辑。
"""

from app.platform.streaming.event_stream import (
    DEFAULT_HEARTBEAT_INTERVAL,
    EventSink,
    ExtraTask,
    StreamResult,
    heartbeat,
    run_agent_stream,
    sse,
)

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "EventSink",
    "ExtraTask",
    "StreamResult",
    "heartbeat",
    "run_agent_stream",
    "sse",
]
