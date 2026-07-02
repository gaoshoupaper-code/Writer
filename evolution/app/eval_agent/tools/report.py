"""报告产出类工具（决策 T4/S14：评估只诊断不提方案）。

评估 Agent 最终一步：组装评分 + 诊断条目 + 证据，写入 evaluation_sessions 表。
铁律：产出里不含任何改进建议/suggestion 字段（那是进化 Agent 方案阶段的活）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import tool

from app.common.flow_metrics import compute_flow_metrics
from app.eval_agent import repo as eval_repo
from app.eval_agent.ctx import get_eval_context
from app.eval_agent.tools.content import get_content_task_result
from app.view.traces import get_trace

logger = logging.getLogger("evolution.eval_agent.tools.report")


def make_report_tools() -> list:
    """构建报告产出类工具。"""

    @tool
    def write_eval_report(findings_json: str, summary: str) -> str:
        """产出评估报告并写入数据库。这必须是评估 Agent 最后一步。

        评估只做诊断（评分 + 问题清单 + 证据），不提改进方案（改进方案归进化 Agent）。

        Args:
            findings_json: 诊断条目 JSON 数组字符串。每条含：
              {
                "dimension": "协作拓扑|错误保障|资源消耗|内容质量",
                "severity": "high|medium|low",
                "evidence_type": "实证|推断",   # 实证=有trace证据；推断=基于常识的判断
                "finding": "问题描述",
                "evidence": "trace 证据（节点id/指标值）"
              }
              注意：不要包含 suggestion/改进建议 字段（评估只诊断不提方案）。
            summary: 自然语言总述（整体评估结论：主要问题在哪、严重程度如何，
              不要写"该怎么改"）
        """
        ctx = get_eval_context()
        if ctx is None:
            return "错误：评估 session 未初始化"
        trace_id = ctx.trace_id
        try:
            findings = json.loads(findings_json)
            if not isinstance(findings, list):
                return "findings_json 必须是 JSON 数组"
            # 去除每条 finding 里可能误带的 suggestion 字段（T4/S14：只诊断不提方案）
            for f in findings:
                if isinstance(f, dict):
                    f.pop("suggestion", None)

            # 取内容分数（后台任务可能已完成）—— 只读访问 content 状态
            content_scores: dict[str, Any] = {}
            cr = get_content_task_result(trace_id)
            if cr and not cr.get("error") and not cr.get("skipped"):
                content_scores = cr

            # 算流程硬指标（D8：自动算，写进报告）
            detail = get_trace(trace_id)
            flow_metrics = compute_flow_metrics(detail)

            # 组装 scores（内容层 + 流程硬指标）
            scores = {
                "content": content_scores,
                "flow_metrics": flow_metrics,
            }

            # 组装可读报告全文（report_md）
            lines = [f"# 评估报告（trace={trace_id}）", "", summary, ""]
            if findings:
                lines.append("## 诊断条目")
                lines.append("")
                for i, f in enumerate(findings, 1):
                    sev = f.get("severity", "?")
                    dim = f.get("dimension", "?")
                    lines.append(f"### {i}. [{sev.upper()}] {dim}")
                    lines.append(f"- **类型**：{f.get('evidence_type', '?')}")
                    lines.append(f"- **发现**：{f.get('finding', '')}")
                    lines.append(f"- **证据**：{f.get('evidence', '')}")
                    lines.append("")
            report_md = "\n".join(lines)

            # 写入 evaluation_sessions 表（S2 DB 交接）
            eval_repo.update_session(
                ctx.eval_id,
                status="done",
                scores=scores,
                findings=findings,
                report_md=report_md,
            )
            ctx.emit_step(
                "write_eval_report", "done", findings=len(findings),
            )
            return f"评估报告已产出并入库（{len(findings)} 条诊断）"
        except json.JSONDecodeError as e:
            ctx.emit_step("write_eval_report", "failed", error=str(e))
            return f"findings_json 解析失败：{e}"
        except Exception as e:
            ctx.emit_step("write_eval_report", "failed", error=str(e))
            return f"产出报告失败：{e}"

    return [write_eval_report]


__all__ = ["make_report_tools"]
