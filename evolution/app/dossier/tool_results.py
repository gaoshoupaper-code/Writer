"""Trace 工具终态的统一成功语义。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.models import TraceLogEvent


def is_successful_tool_end(event: TraceLogEvent) -> bool:
    """排除 DeepAgents 以 ToolMessage(status=error) 正常返回的工具失败。"""
    if event.type != "tool_end" or event.status != "completed":
        return False
    output: Any = event.tool_output
    result_status = output.get("status") if isinstance(output, Mapping) else getattr(
        output, "status", None
    )
    return str(result_status or "").lower() != "error"


__all__ = ["is_successful_tool_end"]
