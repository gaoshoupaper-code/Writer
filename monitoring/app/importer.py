"""trace 摄入器：jsonl events → 投影 → 写入 SQLite 三表。

数据流：read_events → 推导 run summary → projector 投影 → 写 runs/nodes/event_payloads。
完全从 events 自洽推导，不依赖后端的 index.json（那是后端运行态）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import app.db as db
from app import loader, projector
from app.models import TraceLogEvent, TraceNode, TraceRunSummary, TraceUsage


def ingest_trace(trace_path: Path, workspace_id_hint: str | None = None) -> str | None:
    """摄入一个 trace jsonl：投影 + 入库。

    Args:
        trace_path: trace jsonl 文件绝对路径。
        workspace_id_hint: 可选的 workspace_id 提示（兜底扫描时已知，避免空值）。

    Returns:
        摄入的 trace_id；若文件无有效事件则返回 None。
    """
    events = loader.read_events(trace_path)
    if not events:
        return None

    run, owner_user_id = _derive_run_summary(events, trace_path, workspace_id_hint)

    # 幂等：同 trace_id 重复摄入先删旧记录（trace_flags/nodes/events 随 ON DELETE CASCADE）
    db.execute("DELETE FROM runs WHERE trace_id = ?", (run.trace_id,))

    _write_run(run, owner_user_id)
    _write_events(run.trace_id, events)
    _write_nodes(run.trace_id, run, events)

    # 摄入完成后跑规则标红（数据已就绪）
    try:
        from app.rules_engine import evaluate_trace
        evaluate_trace(run.trace_id)
    except Exception:
        # 规则评估失败不影响摄入主流程
        pass

    return run.trace_id


def _derive_run_summary(
    events: list[TraceLogEvent], trace_path: Path, workspace_id_hint: str | None
) -> TraceRunSummary:
    """从 events 自洽推导 TraceRunSummary。"""
    run_start = next((e for e in events if e.type == "run_start"), None)
    run_end = next((e for e in events if e.type == "run_end"), None)
    run_error = next((e for e in events if e.type == "run_error"), None)

    # run_start 的 input 携带 endpoint/thread_id/workspace_id/session_name
    start_input = (run_start.input if run_start and isinstance(run_start.input, dict) else {}) or {}

    trace_id = events[0].trace_id
    started_at = run_start.timestamp if run_start else events[0].timestamp
    ended_at: str | None = None
    status: str = "failed"   # 默认 failed：既无 run_end 也无 run_error = 异常终止（未正常收尾）
    duration_ms: int | None = None
    error: str | None = None

    if run_end:
        ended_at = run_end.timestamp
        status = run_end.status if run_end.status != "running" else "completed"
        duration_ms = run_end.duration_ms
    elif run_error:
        ended_at = run_error.timestamp
        status = "failed"
        duration_ms = run_error.duration_ms
        error = run_error.error

    # workspace_id：优先 run_start.input，其次 hint
    workspace_id = str(start_input.get("workspace_id") or workspace_id_hint or "unknown")

    # owner_user_id（Phase 3 D2/D20）：从 run_start.input 提取，缺省 'unknown'(T7)。
    owner_user_id = str(start_input.get("user_id") or "unknown")

    return TraceRunSummary(
        trace_id=trace_id,
        workspace_id=workspace_id,
        thread_id=str(start_input.get("thread_id") or ""),
        session_name=str(start_input.get("session_name") or ""),
        workspace_path=str(trace_path.parent.parent),  # workspace/<工作区>/ 根
        endpoint=str(start_input.get("endpoint") or ""),
        status=status,  # type: ignore[arg-type]
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        event_count=len(events),
        path=str(trace_path),
        error=error,
    ), owner_user_id


def _write_run(run: TraceRunSummary, owner_user_id: str = "unknown") -> None:
    db.execute(
        """INSERT INTO runs
           (trace_id, workspace_id, thread_id, session_name, endpoint, status,
            started_at, ended_at, duration_ms, event_count, error, ingested_at,
            owner_user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run.trace_id, run.workspace_id, run.thread_id, run.session_name, run.endpoint,
            run.status, run.started_at, run.ended_at, run.duration_ms, run.event_count,
            run.error, datetime.now(UTC).isoformat(),
            owner_user_id,
        ),
    )


def _write_events(trace_id: str, events: list[TraceLogEvent]) -> None:
    rows = [
        (trace_id, e.sequence, e.type, e.timestamp, json.dumps(e.model_dump(), ensure_ascii=False))
        for e in events
    ]
    db.executemany(
        """INSERT INTO event_payloads (trace_id, sequence, type, timestamp, payload_json)
           VALUES (?, ?, ?, ?, ?)""",
        rows,
    )


def _write_nodes(trace_id: str, run: TraceRunSummary, events: list[TraceLogEvent]) -> None:
    """投影 events → nodes 树 → 写入 nodes 表。"""
    projection = projector.TraceProjector().project(run, events)
    rows = [_node_row(trace_id, node) for node in projection.nodes]
    if rows:
        db.executemany(
            """INSERT INTO nodes
               (trace_id, node_id, parent_node_id, kind, label, status,
                agent_name, agent_role, depth, started_at, ended_at, duration_ms,
                model_name, tool_name, skill_name,
                usage_input, usage_output, usage_total,
                chain_summary, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def _node_row(trace_id: str, node: TraceNode) -> tuple[Any, ...]:
    usage = node.usage or TraceUsage()
    return (
        trace_id, node.node_id, node.parent_node_id, node.kind, node.label, node.status,
        node.agent_name, node.agent_role, node.depth,
        node.started_at, node.ended_at, node.duration_ms,
        node.model_name, node.tool_name, node.skill_name,
        usage.input_tokens, usage.output_tokens, usage.total_tokens,
        node.chain_summary, node.error,
    )
