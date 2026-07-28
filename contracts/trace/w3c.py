"""W3C Trace Context 解析与 Writer 外部关联引用。"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass


_TRACEPARENT_RE = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)


@dataclass(frozen=True)
class ParsedTraceparent:
    trace_id: str
    parent_span_id: str
    trace_flags: str


@dataclass(frozen=True)
class W3CTraceContext:
    trace_id: str
    span_id: str
    trace_flags: str
    parent_span_id: str | None = None

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    @property
    def external_refs(self) -> dict[str, str]:
        refs = {
            "w3c_trace_id": self.trace_id,
            "w3c_span_id": self.span_id,
            "traceparent": self.traceparent,
        }
        if self.parent_span_id:
            refs["w3c_parent_span_id"] = self.parent_span_id
        return refs


def parse_traceparent(value: str | None) -> ParsedTraceparent | None:
    if not value:
        return None
    match = _TRACEPARENT_RE.fullmatch(value.strip().lower())
    if match is None:
        return None
    trace_id, parent_span_id, trace_flags = match.groups()
    if int(trace_id, 16) == 0 or int(parent_span_id, 16) == 0:
        return None
    return ParsedTraceparent(trace_id, parent_span_id, trace_flags)


def create_trace_context(incoming_traceparent: str | None = None) -> W3CTraceContext:
    parent = parse_traceparent(incoming_traceparent)
    return W3CTraceContext(
        trace_id=parent.trace_id if parent else secrets.token_hex(16),
        span_id=secrets.token_hex(8),
        trace_flags=parent.trace_flags if parent else "01",
        parent_span_id=parent.parent_span_id if parent else None,
    )


__all__ = [
    "ParsedTraceparent", "W3CTraceContext", "parse_traceparent", "create_trace_context",
]
