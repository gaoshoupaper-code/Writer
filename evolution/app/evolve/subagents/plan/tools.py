"""方案子代理工具集（决策 D11/E16/E14 + S13）。

- read_eval_report()   读当前 session 的评估报告（从 ctx.eval_snapshot）
- read_trace(trace_id) 读 trace 摘要（进化方案阶段自主分析，S13）
- write_design_doc(changes_json, rationale)  产出 design_doc.md
"""
from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

from app.evolve import docs
from app.evolve.ctx import get_tool_context
from app.view.traces import get_trace

logger = logging.getLogger("evolution.evolve.plan.tools")


def make_plan_tools() -> list:
    """构建方案子代理的工具集（S13：含 read_trace + read_eval_report 改 DB）。"""

    @tool
    def read_eval_report() -> str:
        """读取当前 session 的评估报告（由评估 Agent 产出，已加载到上下文）。

        评估报告包含：
          - scores：内容层评分 + 流程硬指标（协作拓扑/错误保障/资源消耗）
          - findings：诊断条目（每条含 dimension/severity/evidence_type/finding/evidence）
          - report_md：可读报告全文

        注意：评估只诊断问题（不含改进方案）。你据此设计改进方案。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        if not ctx.eval_snapshot:
            return "错误：评估报告未加载（eval_snapshot 为空）"
        try:
            snap = ctx.eval_snapshot
            scores = snap.get("scores", {})
            findings = snap.get("findings", [])
            report_md = snap.get("report_md", "")
            return (
                f"## 评估报告（trace={snap.get('trace_id', '?')}）\n\n"
                f"### 结构化分数\n```json\n{json.dumps(scores, ensure_ascii=False, indent=2)}\n```\n\n"
                f"### 诊断条目\n```json\n{json.dumps(findings, ensure_ascii=False, indent=2)}\n```\n\n"
                f"### 报告正文\n{report_md}"
            )
        except Exception as e:
            return f"读评估报告失败：{e}"

    @tool
    def read_trace(trace_id: str) -> str:
        """读取一个 trace 的节点结构化摘要（进化方案阶段自主分析用，S13）。

        用于看实际执行流程，对照评估诊断理解问题。需要更多细节时，
        用框架自带的 read_file 读 trace 相关文件。

        Args:
            trace_id: 要读的 trace id
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("read_trace", "running", phase="plan", trace_id=trace_id)
        try:
            detail = get_trace(trace_id)
            run = detail.run
            lines = [
                f"trace_id: {run.trace_id}",
                f"状态: {run.status}  耗时: {run.duration_ms or '?'}ms  事件数: {run.event_count}",
            ]
            if run.error:
                lines.append(f"错误: {run.error[:300]}")
            lines.append(f"节点数: {len(detail.nodes)}")
            for node in detail.nodes:
                if node.kind == "run":
                    continue
                parts = [f"  [{node.kind}]"]
                if node.node_id:
                    parts.append(f"id={node.node_id}")
                if node.agent_name:
                    parts.append(node.agent_name)
                if node.tool_name:
                    parts.append(f"tool={node.tool_name}")
                if node.status and node.status != "ok":
                    parts.append(f"status={node.status}")
                if node.error:
                    parts.append(f"err={node.error[:120]}")
                if node.chain_summary:
                    parts.append(f"| {node.chain_summary[:160]}")
                lines.append(" ".join(parts))
            ctx.emit_step("read_trace", "done", phase="plan", trace_id=trace_id)
            return "\n".join(lines)
        except Exception as e:
            ctx.emit_step("read_trace", "failed", phase="plan", error=str(e))
            return f"读 trace 失败：{e}"

    @tool
    def write_design_doc(changes_json: str, rationale: str) -> str:
        """产出改动设计文档 design_doc.md。这必须是方案子代理最后一步。

        你可基于评估诊断一次提出多个方向的改动（批量落地，接受归因模糊）。
        每个改动必须是可被执行子代理落地的具体指令。

        Args:
            changes_json: 改动列表 JSON 数组字符串。每条含：
              {
                "target": "目标（agent/section/key 或源码路径）",
                "change_desc": "改什么（描述性）",
                "reason": "依据评估证据（自然语言）",
                "evidence_ref": ["f01"],  # 必填：引用评估 finding 的 id（read_eval_report 可查）
                "expected_up": "预期涨的方面",
                "expected_down": "预期跌的方面（诚实声明）",
                "edit": {  # 可选：直接给 apply_edits 的指令
                    "op": "replace|insert|remove",
                    "target": ["agent名", "processors|slots", key],
                    "spec": {"class": "类名", "params": {...}}
                }
              }
              **evidence_ref 是硬性必填**：每个改动必须引用至少一个评估 finding 的 id，
              证明"为什么改"。id 格式 f01/f02…，从 read_eval_report 的 findings 里取。
              一个改动可引多个 finding（一条改动同时解决多个问题）。
            rationale: 自然语言总述（基于评估报告的整体判断，为什么这么改）
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("write_design_doc", "running", phase="plan")
        try:
            changes = json.loads(changes_json)
            if not isinstance(changes, list):
                return "changes_json 必须是 JSON 数组"
            if not changes:
                return "changes 不能为空（至少一个改动）"

            # 提取评估报告里的合法 finding id 集合（evidence_ref 校验依据）
            valid_finding_ids: set[str] = set()
            snap = ctx.eval_snapshot or {}
            findings = snap.get("findings") or []
            for f in findings:
                if isinstance(f, dict) and f.get("id"):
                    valid_finding_ids.add(str(f["id"]))

            # 死局短路（R6+）：评估报告无可用 finding id 时，evidence_ref 校验不可满足。
            # 此时正常校验路径会反复返回"合法 id：（无）"让 Agent 误以为重读报告就能解决，
            # 陷入无意义重试（实测一次进化可空转 19 次）。提前明确告知这是死局，让 Agent
            # 停止尝试、直接结束，由 PhaseGuard 记 fail 并提示用户重新评估。
            if not valid_finding_ids:
                ctx.emit_step("write_design_doc", "failed", phase="plan", reason="no_findings")
                return (
                    "评估报告没有可引用的结构化 finding（findings 为空或无 id）。"
                    "evidence_ref 校验无法满足，无法产出 design_doc。"
                    "这是评估阶段的问题——请结束当前方案，提示重新评估该 trace 后再启动进化。"
                    "不要重试 write_design_doc，不要尝试不同的 id 格式。"
                )

            # 校验：每个 change 必须有 target + change_desc + reason + evidence_ref
            for i, c in enumerate(changes):
                if not isinstance(c, dict):
                    return f"changes[{i}] 必须是对象"
                for field in ("target", "change_desc", "reason"):
                    if not c.get(field):
                        return f"changes[{i}] 缺少必填字段：{field}"
                # evidence_ref 硬校验（R6：缺证据不让过）
                refs = c.get("evidence_ref")
                if not isinstance(refs, list) or not refs:
                    return (
                        f"changes[{i}] 缺少 evidence_ref：每个改动必须引用至少一个评估 "
                        f"finding 的 id（如 [\"f01\"]）。请先 read_eval_report 拿到 finding id。"
                    )
                bad = [r for r in refs if str(r) not in valid_finding_ids]
                if bad:
                    return (
                        f"changes[{i}] 的 evidence_ref {bad} 不存在于评估报告的 finding 列表中。"
                        f"合法 id：{sorted(valid_finding_ids) or '（无）'}。"
                        f"请用 read_eval_report 确认正确的 finding id。"
                    )

            path = docs.write_design_doc(
                ctx.session_id,
                changes=changes,
                rationale=rationale,
            )
            ctx.design_doc_path = path
            from app.evolve import db as ev_db
            ev_db.update_session(ctx.session_id, design_doc_path=path)
            ctx.emit_step(
                "write_design_doc", "done", phase="plan",
                path=path, changes=len(changes),
            )
            return f"设计文档已产出：{path}（{len(changes)} 个改动）"
        except json.JSONDecodeError as e:
            ctx.emit_step("write_design_doc", "failed", phase="plan", error=str(e))
            return f"changes_json 解析失败：{e}"
        except Exception as e:
            ctx.emit_step("write_design_doc", "failed", phase="plan", error=str(e))
            return f"产出设计文档失败：{e}"

    return [read_eval_report, read_trace, write_design_doc]


__all__ = ["make_plan_tools"]
