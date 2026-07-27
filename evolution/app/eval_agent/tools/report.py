"""报告产出类工具（决策 T4/S14：评估只诊断不提方案）。

评估 Agent 最终一步：组装评分 + 诊断条目 + 证据，封存为不可变评估卷宗
（evaluation_dossiers 表，阶段 C）。
铁律：产出里不含任何改进建议/suggestion 字段（那是进化 Agent 方案阶段的活）。

阶段 C 切断：flow_metrics 从证据卷宗 facts 读（B 阶段已冻结），
不再 load_trace_detail 直读 trace。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import tool

from app.eval_agent import repo as eval_repo
from app.eval_agent.ctx import get_eval_context
from app.eval_agent.tools.content import get_content_task_result

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
              每条 finding 的 id 由工具强制生成（f01/f02…），你无需填写。
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
            # 规范化每条 finding：去 suggestion + 强制重编号 id（进化端 evidence_ref 依赖稳定 id）
            for i, f in enumerate(findings, 1):
                if isinstance(f, dict):
                    f.pop("suggestion", None)
                    f["id"] = f"f{i:02d}"  # 强制覆盖，杜绝 Agent 产出格式不一致

            # 取内容分数（后台任务可能已完成）—— 只读访问 content 状态
            content_scores: dict[str, Any] = {}
            cr = get_content_task_result(ctx.eval_id)
            if cr and not cr.get("error") and not cr.get("skipped"):
                content_scores = cr

            # 流程硬指标从证据卷宗 facts 读（阶段 C 切断 load_trace_detail 旁路）。
            # B 阶段已把 topology/reliability/resources 冻结进卷宗 facts。
            dossier = ctx.dossier or {}
            facts = dossier.get("facts") or {}
            flow_metrics = {
                "topology": facts.get("topology", {}),
                "reliability": facts.get("reliability", {}),
                "resources": facts.get("resources", {}),
            }

            # 组装 scores（第三期 28 维五级锚点结构）
            from app.eval_agent.rubrics import xianxia as rubric
            scores = {
                "rubric_version": content_scores.get("rubric_version", rubric.RUBRIC_VERSION),
                "calibration": content_scores.get("calibration", rubric.CALIBRATION_STATUS),
                "groups": content_scores.get("groups", {}),
                "badcase": content_scores.get("badcase", {}),
                "flow_metrics": flow_metrics,
            }

            # 组装可读报告全文（report_md）
            lines = [f"# 评估报告（trace={trace_id}）", "", summary, ""]
            if findings:
                lines.append("## 诊断条目")
                lines.append("")
                for f in findings:
                    fid = f.get("id", "?")
                    sev = f.get("severity", "?")
                    dim = f.get("dimension", "?")
                    lines.append(f"### {fid} [{sev.upper()}] {dim}")
                    lines.append(f"- **类型**：{f.get('evidence_type', '?')}")
                    lines.append(f"- **发现**：{f.get('finding', '')}")
                    lines.append(f"- **证据**：{f.get('evidence', '')}")
                    lines.append("")
            report_md = "\n".join(lines)

            # 封存为不可变评估卷宗（阶段 C）。
            # sealer 做完整性校验 + 原子写入 evaluation_dossiers + 回填尝试 completed。
            # 封存失败 = 评估失败（R9），抛 SealError 由调用方标 failed。
            from app.eval_agent.sealer import seal_evaluation_dossier, collect_frozen_evidence, SealError

            # 从卷宗拿 owner_user_id（评估卷宗继承证据卷宗血缘）
            dossier = ctx.dossier or {}
            owner_user_id = dossier.get("owner_user_id") or "unknown"

            # 阶段 D：冻结 finding 引用的证据片段进评估卷宗（供进化归因，需求 §22）。
            # 只冻结证据卷宗 index 登记的 ID（受控回钻边界）。
            index = dossier.get("index") or {}
            allowed_evidence_ids = set(index.get("evidence_ids", []))
            frozen_evidence = collect_frozen_evidence(
                findings, [], trace_id, allowed_evidence_ids,
            )

            try:
                evd_id = seal_evaluation_dossier(
                    eval_attempt_id=ctx.eval_id,
                    source_dossier_id=ctx.dossier_id,
                    source_dossier_version=ctx.dossier_version or 0,
                    trace_id=trace_id,
                    owner_user_id=owner_user_id,
                    conclusions=[],  # 首期结论内联在 report_md，findings 是结构化核心
                    findings=findings,
                    positive_patterns=[],
                    scores=scores,
                    report_md=report_md,
                    frozen_evidence=frozen_evidence,
                )
            except SealError as e:
                ctx.emit_step("write_eval_report", "failed", error=str(e))
                # 封存失败：尝试标 failed（R9，不产生分裂态）
                eval_repo.update_session(ctx.eval_id, status="failed", failure_reason=str(e)[:500])
                return f"评估卷宗封存失败，本次评估未成功：{e}"

            ctx.emit_step(
                "write_eval_report", "done", findings=len(findings), evd=evd_id,
            )
            return f"评估卷宗已封存（{len(findings)} 条诊断，evd={evd_id[:8]}）"
        except json.JSONDecodeError as e:
            ctx.emit_step("write_eval_report", "failed", error=str(e))
            return f"findings_json 解析失败：{e}"
        except Exception as e:
            ctx.emit_step("write_eval_report", "failed", error=str(e))
            return f"产出报告失败：{e}"

    return [write_eval_report]


__all__ = ["make_report_tools"]
