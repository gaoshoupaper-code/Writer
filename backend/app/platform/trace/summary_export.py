"""模型可读的执行记录导出。

每条 trace 结束后自动生成一份摘要 JSON 文件，与原始 JSONL 同目录。
摘要粒度为节点级别（agent/llm/tool/todo/error），每个节点只保留关键信息，
便于 LLM 高效阅读。通过 node_id / raw_event_ids 可回查原始 trace JSONL 获取完整数据。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.platform.trace.schemas import TraceDetail, TraceNode


def export_trace_summary(detail: TraceDetail, output_path: Path) -> None:
    """将 TraceDetail 导出为模型可读的摘要 JSON。

    Args:
        detail: 已投影的 trace 详情。
        output_path: 摘要文件输出路径（如 traces/20260608-1430/trace_xxx_summary.json）。
    """
    summary = _build_summary(detail)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


def _build_summary(detail: TraceDetail) -> dict[str, Any]:
    """构建摘要结构。"""
    run = detail.run
    return {
        "trace_id": run.trace_id,
        "endpoint": run.endpoint,
        "session_name": run.session_name,
        "status": run.status,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "duration_ms": run.duration_ms,
        "error": run.error,
        "node_count": len(detail.nodes),
        "nodes": [_node_summary(node) for node in detail.nodes if node.kind != "run"],
    }


def _node_summary(node: TraceNode) -> dict[str, Any]:
    """单个节点的摘要（几十 token）。"""
    summary: dict[str, Any] = {
        "node_id": node.node_id,
        "kind": node.kind,
        "status": node.status,
    }

    # 可选字段——仅非 None 时写入以节省 token
    if node.agent_name:
        summary["agent"] = node.agent_name
    if node.label and node.kind != "agent":
        summary["label"] = node.label
    if node.duration_ms is not None:
        summary["duration_ms"] = node.duration_ms
    if node.error:
        summary["error"] = _truncate(node.error, 200)
    if node.chain_summary:
        summary["summary"] = _truncate(node.chain_summary, 150)

    # LLM 节点：模型名 + token 用量
    if node.model_name:
        summary["model"] = node.model_name
    if node.usage:
        summary["tokens"] = {
            "input": node.usage.input_tokens,
            "output": node.usage.output_tokens,
        }

    # Tool 节点：工具名
    if node.tool_name:
        summary["tool"] = node.tool_name

    # 索引——用于回查原始 JSONL
    if node.raw_event_ids:
        summary["event_ids"] = node.raw_event_ids

    return summary


def _truncate(text: str, max_len: int) -> str:
    return text[:max_len] + "…" if len(text) > max_len else text
