"""platform.trace —— 领域无关的 trace 子系统（PR-05 迁入）。

trace 是通用基础设施（writing/image 都用），从 writer/trace 物理迁入此目录。
包含：recorder / projector / chain_summary / schemas / summary_export。

writer/trace/__init__.py 保留 re-export 门面供旧代码过渡，PR-11 writer 降级时清理。
"""

from app.platform.trace.recorder import TraceRecorder, TraceRunHandle
from app.platform.trace.schemas import (
    TraceAgentRole,
    TraceContextKind,
    TraceContextRange,
    TraceContextSegment,
    TraceDetail,
    TraceEventType,
    TraceLogEvent,
    TraceNode,
    TraceNodeKind,
    TraceRunSummary,
    TraceStatus,
    TraceTodoItem,
    TraceTodoSnapshot,
    TraceTodoStatus,
    TraceUsage,
)

__all__ = [
    "TraceAgentRole",
    "TraceContextKind",
    "TraceContextRange",
    "TraceContextSegment",
    "TraceDetail",
    "TraceEventType",
    "TraceLogEvent",
    "TraceNode",
    "TraceNodeKind",
    "TraceRecorder",
    "TraceRunHandle",
    "TraceRunSummary",
    "TraceStatus",
    "TraceTodoItem",
    "TraceTodoSnapshot",
    "TraceTodoStatus",
    "TraceUsage",
]
