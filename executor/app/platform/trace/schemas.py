"""trace schema —— 从共享契约层 contracts/trace re-export。

单一真源已迁至 contracts/trace/（Writer 仓库顶层共享包，执行端与进化端共用）。
本文件保留为过渡兼容入口，executor 内部 `from app.platform.trace.schemas import X`
继续有效，但实际定义在 contracts.trace。

修改 trace 字段请改 contracts/trace/__init__.py，不要改这里。
"""

from __future__ import annotations

from contracts.trace import (
    TraceAgentRole,
    TraceContextKind,
    TraceContextRange,
    TraceContextSegment,
    TraceDetail,
    TraceEventType,
    TraceIntegrityStatus,
    TraceLogEvent,
    TraceManifest,
    TracePayloadKind,
    TracePayloadRef,
    TraceNode,
    TraceNodeKind,
    TraceRunSummary,
    TraceStatus,
    TraceSpanLink,
    TraceTodoItem,
    TraceTodoSnapshot,
    TraceTodoStatus,
    TraceUsage,
    TraceWorkload,
    TraceCoverageStatus,
    SkillCatalogEntry,
    MiddlewareDescriptor,
)

__all__ = [
    "TraceAgentRole",
    "TraceContextKind",
    "TraceContextRange",
    "TraceContextSegment",
    "TraceDetail",
    "TraceEventType",
    "TraceIntegrityStatus",
    "TraceLogEvent",
    "TraceManifest",
    "TracePayloadKind",
    "TracePayloadRef",
    "TraceNode",
    "TraceNodeKind",
    "TraceRunSummary",
    "TraceStatus",
    "TraceSpanLink",
    "TraceTodoItem",
    "TraceTodoSnapshot",
    "TraceTodoStatus",
    "TraceUsage",
    "TraceWorkload",
    "TraceCoverageStatus",
    "SkillCatalogEntry",
    "MiddlewareDescriptor",
]
