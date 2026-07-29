"""Trace 取消投影测试（EVD-014, EDGE-008, FR-006）。

验证 Phase 6 根因修复：
  1. cancel_requested / run_cancelled / cancel_timeout 事件投影为可见控制节点
     （旧 projector 完全丢弃 run_cancelled，取消后无节点可看）。
  2. 未配对的 llm_start / tool_start 在 cancelled/interrupted run 下投影为
     cancelled/interrupted，不硬编码 running（EDGE-008）。

跑法（在 evolution 目录）：
    python -m pytest tests/test_cancel_projection.py -v
"""

from __future__ import annotations

import unittest

from app.core.models import TraceRunSummary
from app.ingestion.projector import TraceProjector
from contracts.trace import TraceLogEvent


TRACE_ID = "trace-cancel-proj"


def _event(sequence: int, event_type: str, *, status: str = "running", **kw) -> TraceLogEvent:
    return TraceLogEvent(
        trace_id=TRACE_ID,
        event_id=f"evt-{sequence}",
        sequence=sequence,
        type=event_type,
        status=status,
        timestamp=f"2026-07-29T05:32:4{sequence % 10}+00:00",
        source="runtime",
        schema_version=2,
        **kw,
    )


def _run(status: str) -> TraceRunSummary:
    return TraceRunSummary(
        trace_id=TRACE_ID,
        workspace_id="ws", thread_id="th", session_name="s",
        workspace_path="", endpoint="ab", status=status,
        started_at="2026-07-29T05:32:40+00:00",
        event_count=4, path="", schema_version=2,
        service="executor", workload="creation", purpose="evolution",
        integrity_status="verified", trace_phase="sealed",
    )


class CancelProjectionTest(unittest.TestCase):
    def test_cancel_events_projected_as_visible_nodes(self) -> None:
        """EVD-014：cancel_requested + run_cancelled 投影为可见控制节点。"""
        events = [
            _event(1, "run_start", status="running"),
            _event(2, "llm_start", status="running", run_id="llm-1", model_name="m"),
            _event(3, "cancel_requested", status="cancelling", error="User stopped"),
            _event(4, "run_cancelled", status="cancelled", error="User stopped"),
        ]
        proj = TraceProjector().project(_run("cancelled"), events)
        labels = [n.label for n in proj.nodes]
        # 取消控制节点必须可见。
        self.assertIn("用户请求取消", labels)
        self.assertIn("已取消", labels)

    def test_cancel_timeout_projected(self) -> None:
        """cancel_timeout 投影为"取消超时"节点（EDGE-007 诚实告警可见）。"""
        events = [
            _event(1, "run_start", status="running"),
            _event(2, "cancel_timeout", status="cancel_timeout", error="未收敛"),
        ]
        proj = TraceProjector().project(_run("cancel_timeout"), events)
        labels = [n.label for n in proj.nodes]
        self.assertIn("取消超时（未收敛）", labels)

    def test_unpaired_llm_start_in_cancelled_run_not_running(self) -> None:
        """EDGE-008：cancelled run 下未配对 llm_start 投影为 cancelled，不显示 running。"""
        events = [
            _event(1, "run_start", status="running"),
            _event(2, "llm_start", status="running", run_id="llm-1", model_name="m"),
            # 无 llm_end——子进程被强杀，只持久化了 start。
            _event(3, "run_cancelled", status="cancelled"),
        ]
        proj = TraceProjector().project(_run("cancelled"), events)
        llm_nodes = [n for n in proj.nodes if n.kind == "llm"]
        self.assertTrue(len(llm_nodes) >= 1)
        self.assertEqual(llm_nodes[0].status, "cancelled")  # 非 running
        self.assertNotIn("运行中", llm_nodes[0].chain_summary or "")

    def test_unpaired_tool_start_in_interrupted_run(self) -> None:
        """EDGE-008：interrupted run 下未配对 tool_start 投影为 interrupted。"""
        events = [
            _event(1, "run_start", status="running"),
            _event(2, "tool_start", status="running", run_id="tool-1", tool_name="search"),
            # 无 tool_end——中断。
        ]
        proj = TraceProjector().project(_run("interrupted"), events)
        tool_nodes = [n for n in proj.nodes if n.kind == "tool"]
        self.assertTrue(len(tool_nodes) >= 1)
        self.assertEqual(tool_nodes[0].status, "interrupted")

    def test_unpaired_start_in_running_run_still_running(self) -> None:
        """真活跃 run（running）下未配对 start 仍投影为 running（不变）。"""
        events = [
            _event(1, "run_start", status="running"),
            _event(2, "llm_start", status="running", run_id="llm-1", model_name="m"),
        ]
        proj = TraceProjector().project(_run("running"), events)
        llm_nodes = [n for n in proj.nodes if n.kind == "llm"]
        self.assertEqual(llm_nodes[0].status, "running")

    def test_run_node_includes_cancelled_in_raw_event_ids(self) -> None:
        """_run_node 的 raw_event_ids 包含 run_cancelled（旧代码排除它，导致取消事件断链）。"""
        events = [
            _event(1, "run_start", status="running"),
            _event(2, "run_cancelled", status="cancelled"),
        ]
        proj = TraceProjector().project(_run("cancelled"), events)
        run_node = next(n for n in proj.nodes if n.kind == "run")
        self.assertIn("evt-2", run_node.raw_event_ids)  # run_cancelled 的 event_id


if __name__ == "__main__":
    unittest.main()
