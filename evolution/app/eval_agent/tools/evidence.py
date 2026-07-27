"""证据卷宗读取类工具（阶段 C：可见性隔离后）。

评估 Agent 的唯一事实来源是证据卷宗。本模块提供：
  - read_evidence_pack()    读卷宗评估工作页（清单+重点候选+阶段摘要+review链）
  - drill_evidence(eid)     按证据 ID 受控回钻（只能沿卷宗索引内 ID）

阶段 C 切断：评估 Agent 不再直读原始 trace / 工作区。
卷宗是评估的硬前置——无完整卷宗的评估在启动时即被拒（api 层校验）。
"""
from __future__ import annotations

import logging

from langchain_core.tools import tool

from app.eval_agent.ctx import get_eval_context

logger = logging.getLogger("evolution.eval_agent.tools.evidence")


def make_evidence_tools() -> list:
    """构建证据卷宗读取类工具。"""

    @tool
    def read_evidence_pack() -> str:
        """读取证据卷宗的评估工作页。

        证据卷宗是编译器从 trace 提取的结构化事实底座 + 重点候选。
        先用本工具按 P0→P1→P2 顺序审查重点候选，再用 drill_evidence 回钻证据片段。
        """
        ctx = get_eval_context()
        if ctx is None:
            return "错误：评估 session 未初始化"
        dossier = ctx.dossier
        if not dossier:
            # 阶段 C：无卷宗不应到达此处（启动时已校验）。防御性提示。
            return "错误：证据卷宗未加载（启动时应校验完整卷宗）。"
        ctx.emit_step("read_evidence_pack", "running")
        try:
            eval_view = dossier.get("eval_view") or {}
            manifest = dossier.get("manifest") or {}

            # 格式化评估工作页为可读文本
            lines = ["# 证据卷宗 · 评估工作页", ""]

            # 清单摘要
            ms = eval_view.get("manifest_summary", {})
            lines.append(f"## 任务状态")
            lines.append(f"- run_status: {ms.get('run_status', '?')}")
            lines.append(f"- 完整度: {ms.get('completeness', '?')}")
            lines.append(f"- 适用维度: {', '.join(ms.get('applicable_dimensions', []))}")
            gaps = ms.get("coverage_gaps", [])
            if gaps:
                lines.append(f"- ⚠ 证据缺口: {'; '.join(gaps)}")
            lines.append("")

            # 任务契约覆盖矩阵（B3）
            matrix = manifest.get("contract_coverage_matrix") or {}
            if matrix:
                lines.append(f"## 任务契约覆盖（complete={matrix.get('complete')}）")
                for item in matrix.get("items", [])[:8]:
                    lines.append(
                        f"- [{item.get('status')}] {item.get('dim')}/{item.get('key')}: {item.get('reason', '')[:80]}"
                    )
                lines.append("")

            # 重点候选
            priorities = eval_view.get("priorities", {})
            p0 = priorities.get("P0", [])
            p1 = priorities.get("P1", [])
            p2 = priorities.get("P2", [])
            lines.append(f"## 重点候选（P0:{len(p0)} P1:{len(p1)} P2:{len(p2)}）")
            for p in p0:
                lines.append(f"- **P0** [{p.get('category', '?')}] {p.get('desc', '')} → {p.get('evidence_id', '')}")
            for p in p1:
                lines.append(f"- P1 [{p.get('category', '?')}] {p.get('desc', '')} → {p.get('evidence_id', '')}")
            for p in p2[:5]:  # P2 只展示前 5 条
                lines.append(f"- P2 [{p.get('category', '?')}] {p.get('desc', '')} → {p.get('evidence_id', '')}")
            lines.append("")

            # 阶段摘要
            stages = eval_view.get("stage_summaries", [])
            if stages:
                lines.append("## 阶段摘要")
                for s in stages:
                    if s.get("error"):
                        lines.append(f"- {s.get('stage', '?')}: 归纳失败（{s['error'][:60]}）")
                    else:
                        lines.append(f"- {s.get('stage', '?')}: {s.get('summary', '?')[:200]}")
                lines.append("")

            # review 链
            rcs = eval_view.get("review_chain_summary", [])
            if rcs:
                lines.append("## review 调用链")
                for rc in rcs:
                    lines.append(f"- {rc.get('reviewer', '?')} → {rc.get('review_file', '?')} ({rc.get('evidence_id', '')})")
                lines.append("")

            # 可回钻 ID
            drillable = eval_view.get("drillable_ids", [])
            lines.append(f"## 可回钻证据 ID（共 {len(drillable)} 个）")
            lines.append("用 drill_evidence(evidence_id) 回钻证据片段。")
            if drillable:
                lines.append(f"示例: {', '.join(drillable[:5])}{'...' if len(drillable) > 5 else ''}")
            lines.append("")

            # 指令
            lines.append(f"## 指导")
            lines.append(eval_view.get("instructions", "按 P0→P1→P2 审查。"))

            ctx.emit_step("read_evidence_pack", "done")
            return "\n".join(lines)
        except Exception as e:
            ctx.emit_step("read_evidence_pack", "failed", error=str(e))
            return f"读证据卷宗失败：{e}"

    @tool
    def drill_evidence(evidence_id: str) -> str:
        """按证据 ID 回钻证据片段（受控回钻，限于卷宗索引内 ID）。

        evidence_id 从 read_evidence_pack 的重点候选或可回钻列表获取。
        只能回钻证据卷宗索引层已登记的 ID（受控，不可任意搜索 trace）。

        Args:
            evidence_id: 证据 ID（格式 evt-{event_id}）
        """
        ctx = get_eval_context()
        if ctx is None:
            return "错误：评估 session 未初始化"
        dossier = ctx.dossier
        if not dossier:
            return "错误：证据卷宗未加载"
        ctx.emit_step("drill_evidence", "running", evidence_id=evidence_id)
        try:
            # 校验 evidence_id 在卷宗索引层内（受控回钻）
            index = dossier.get("index") or {}
            allowed = set(index.get("evidence_ids", []))
            if evidence_id not in allowed:
                return (f"证据 ID {evidence_id} 不在卷宗索引内（受控回钻："
                        "只能沿卷宗内 ID 展开）")

            # 从 event_payloads 加载原始事件（受控：ID 已被卷宗索引登记）
            import json
            import app.core.db as db
            event_id = evidence_id[4:] if evidence_id.startswith("evt-") else evidence_id
            row = db.query_one(
                "SELECT payload_json FROM event_payloads WHERE trace_id = ? AND event_id = ?",
                (ctx.input_trace_id, event_id),
            )
            if row is None:
                return f"证据片段 {event_id} 不存在"

            payload = json.loads(row["payload_json"])
            lines = [f"# 证据片段 {evidence_id}", ""]
            lines.append(f"- type: {payload.get('type', '?')}")
            lines.append(f"- agent: {payload.get('agent_name', '?')}")
            lines.append(f"- sequence: {payload.get('sequence', '?')}")
            if payload.get("error"):
                lines.append(f"- error: {payload['error'][:300]}")
            tool_output = payload.get("tool_output")
            if isinstance(tool_output, dict):
                content = tool_output.get("content", "")
                if content:
                    lines.append(f"- tool_output: {str(content)[:800]}")
            output = payload.get("output")
            if output:
                lines.append(f"- output: {str(output)[:800]}")

            ctx.emit_step("drill_evidence", "done")
            return "\n".join(lines)
        except Exception as e:
            ctx.emit_step("drill_evidence", "failed", error=str(e))
            return f"回钻失败：{e}"

    return [read_evidence_pack, drill_evidence]


__all__ = ["make_evidence_tools"]
