"""评估 Agent 工具集聚合入口。

按数据源拆分到 4 子模块（trace/surface/content/report），本 __init__.py
负责把它们聚合为 make_eval_tools()，保持调用方 import 路径不变：
  from app.eval_agent.tools import make_eval_tools, clear_content_tasks
"""
from app.eval_agent.tools.content import (
    clear_content_tasks,
    make_content_tools,
)
from app.eval_agent.tools.report import make_report_tools
from app.eval_agent.tools.surface import make_surface_tools
from app.eval_agent.tools.trace import make_trace_tools


def make_eval_tools() -> list:
    """构建评估 Agent 的完整工具集（聚合 4 子模块）。

    顺序：trace（纵观）→ surface（设计意图）→ content（内容分）→ report（产出）。
    """
    tools: list = []
    tools.extend(make_trace_tools())
    tools.extend(make_surface_tools())
    tools.extend(make_content_tools())
    tools.extend(make_report_tools())
    return tools


__all__ = ["make_eval_tools", "clear_content_tasks"]
