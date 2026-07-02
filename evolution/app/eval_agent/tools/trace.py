"""trace 读取类工具（决策 S13）。

摘要层 + 细节层 + 区间层三件套，供评估 Agent 纵观 trace 全局后定位异常节点。
- read_trace(trace_id)        摘要层：所有节点结构化摘要
- read_trace_node(node_id)     细节层：单节点完整 context（按 anchor 回溯）
- read_trace_range(start,end)  区间层：连续 anchor 区间 context
"""
from __future__ import annotations

import logging

from langchain_core.tools import tool

from app.eval_agent.ctx import get_eval_context
from app.view.traces import get_trace

logger = logging.getLogger("evolution.eval_agent.tools.trace")


def make_trace_tools() -> list:
    """构建 trace 读取类工具。"""

    @tool
    def read_trace(trace_id: str) -> str:
        """【摘要层】读取一个 trace 的所有节点结构化摘要。

        返回每个节点（run/agent/llm/tool/error/skill）的关键信息 + run 元信息。
        不含完整正文——需要细节时用 read_trace_node 或 read_trace_range。
        先用本工具纵观全局，定诊断方向。

        Args:
            trace_id: 要读的 trace id
        """
        ctx = get_eval_context()
        if ctx is None:
            return "错误：评估 session 未初始化"
        ctx.emit_step("read_trace", "running", trace_id=trace_id)
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
            ctx.emit_step("read_trace", "done", trace_id=trace_id)
            return "\n".join(lines)
        except Exception as e:
            ctx.emit_step("read_trace", "failed", error=str(e))
            return f"读 trace 失败：{e}"

    @tool
    def read_trace_node(node_id: str) -> str:
        """【细节层】读取单个节点的完整 context（按 anchor 回溯）。

        当你在 read_trace 摘要里看到某个异常/关键节点时，用它的 node_id 展开
        读完整内容（含 LLM input/output、tool 调用细节）。

        Args:
            node_id: 节点 id（从 read_trace 摘要里获取）
        """
        ctx = get_eval_context()
        if ctx is None:
            return "错误：评估 session 未初始化"
        try:
            trace_id = ctx.trace_id
            detail = get_trace(trace_id)
            # 找节点
            target = None
            for n in detail.nodes:
                if n.node_id == node_id:
                    target = n
                    break
            if not target:
                return f"未找到 node_id={node_id}"
            # 通过 anchor 回溯 context
            lines = [f"节点 {node_id}（{target.kind}）", f"agent: {target.agent_name}"]
            if target.tool_name:
                lines.append(f"tool: {target.tool_name}")
            if target.error:
                lines.append(f"错误: {target.error}")
            # 读关联的 context segments
            related = [
                seg for seg in detail.context
                if seg.related_node_id == node_id or seg.anchor_id == target.context_anchor_id
            ]
            if related:
                lines.append("\n关联 context：")
                for seg in related[:5]:
                    content_str = str(seg.content)[:800]
                    lines.append(f"  [{seg.kind}] {seg.title}: {content_str}")
            else:
                lines.append("（无关联 context，可能需用 read_trace_range 读区间）")
            return "\n".join(lines)
        except Exception as e:
            return f"读节点失败：{e}"

    @tool
    def read_trace_range(anchor_start: str, anchor_end: str) -> str:
        """【区间层】读取连续 anchor 区间的 context（一段时间的完整对话流）。

        当你需要看某段时间的完整流程（如某子代理从头到尾的对话）时用。
        anchor 从 read_trace 摘要或 read_trace_node 获取。

        Args:
            anchor_start: 起始 anchor_id
            anchor_end:   结束 anchor_id
        """
        ctx = get_eval_context()
        if ctx is None:
            return "错误：评估 session 未初始化"
        try:
            trace_id = ctx.trace_id
            detail = get_trace(trace_id)
            # 按 sequence 区间取 context segments
            start_seq = None
            end_seq = None
            for seg in detail.context:
                if seg.anchor_id == anchor_start:
                    start_seq = seg.sequence
                if seg.anchor_id == anchor_end:
                    end_seq = seg.sequence
            if start_seq is None or end_seq is None:
                return f"未找到 anchor 区间 [{anchor_start}, {anchor_end}]"
            ranged = [
                seg for seg in detail.context
                if start_seq <= seg.sequence <= end_seq
            ]
            lines = [f"区间 [{anchor_start} → {anchor_end}]，{len(ranged)} 段 context："]
            for seg in ranged[:20]:
                content_str = str(seg.content)[:400]
                lines.append(f"  [seq={seg.sequence} {seg.kind}] {seg.title}: {content_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"读区间失败：{e}"

    return [read_trace, read_trace_node, read_trace_range]


__all__ = ["make_trace_tools"]
