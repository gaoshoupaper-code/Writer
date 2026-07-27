"""评估 Agent 工具集聚合入口（阶段 C：可见性隔离后）。

阶段 C（2026-07-27）切断原始 trace 直读旁路：评估 Agent 只读证据卷宗。
工具集从 evidence/trace/content/report 收敛为 dossier/content/report：
  - dossier：read_dossier（读评估工作页）+ drill_evidence（受控回钻卷宗内 ID）
  - content：get_content_score（读卷宗冻结正文打分，不再读工作区）
  - report：write_eval_report（读卷宗 facts，产出并封存评估卷宗）

trace.py 的 read_trace/read_trace_node/read_trace_range 已移除（旁路切断）。
"""
from app.eval_agent.tools.content import (
    clear_content_tasks,
    make_content_tools,
)
from app.eval_agent.tools.evidence import make_evidence_tools
from app.eval_agent.tools.report import make_report_tools


def make_eval_tools() -> list:
    """构建评估 Agent 的完整工具集。

    顺序：dossier（证据卷宗，唯一事实源）→ content（读卷宗正文打分）→ report（封存评估卷宗）。
    """
    tools: list = []
    tools.extend(make_evidence_tools())
    tools.extend(make_content_tools())
    tools.extend(make_report_tools())
    return tools


__all__ = ["make_eval_tools", "clear_content_tasks"]
