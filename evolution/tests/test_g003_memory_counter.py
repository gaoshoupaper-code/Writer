"""G003 记忆可数单元测试。

验证 flow_metrics 的 memory 维度从 run_meta/memory_quality 事件正确计数，
且 memory_participated 只看成功召回（retrieval_ok==True），不数失败/降级路径。

设计依据：.claude/md/20260801_192157_进化信息可见性与评估漏判.md G003 + FR-001
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.common.flow_metrics import _memory_metrics, compute_flow_metrics  # noqa: E402


def _evt(event_type: str, input_data=None, event_id="e1", sequence=0):
    """构造一个最小 TraceLogEvent-like 对象。"""
    return SimpleNamespace(
        type=event_type,
        input=input_data if input_data is not None else {},
        event_id=event_id,
        sequence=sequence,
        source="middleware",
        tool_name=None,
        usage=None,
        agent_name="writing",
        error=None,
    )


def _node(name="writing-subagent"):
    return SimpleNamespace(
        kind="agent", agent_name=name, parallel_group_id=None, depth=1,
        duration_ms=100, started_at="2026-01-01T00:00:00",
    )


class MemoryMetricsTest(unittest.TestCase):
    """G003：记忆系统参与指标。"""

    def test_no_memory_events(self):
        """无 memory 埋点 → memory_participated=False, memory_available=False。"""
        events = [_evt("llm_end"), _evt("tool_start", {"path": "x"})]
        m = _memory_metrics(events)
        self.assertEqual(m["memory_recall_events"], 0)
        self.assertEqual(m["memory_recall_ok"], 0)
        self.assertFalse(m["memory_participated"])
        self.assertFalse(m["memory_available"])

    def test_successful_recall_participated(self):
        """成功召回 → memory_participated=True。"""
        events = [_evt("run_meta", {"memory_quality": {"retrieval_ok": True}})]
        m = _memory_metrics(events)
        self.assertEqual(m["memory_recall_ok"], 1)
        self.assertTrue(m["memory_participated"])
        self.assertTrue(m["memory_available"])

    def test_failed_recall_not_participated(self):
        """关键：失败召回（retrieval_ok=False）→ memory_available=True 但 participated=False。

        degraded/失败路径（backend 不健康/健康检查失败/retrieve 抛异常）也写事件，
        不能把"记忆系统挂了"误判成"记忆系统参与了"。
        """
        events = [_evt("run_meta", {"memory_quality": {"retrieval_ok": False, "error": "unhealthy"}})]
        m = _memory_metrics(events)
        self.assertEqual(m["memory_recall_events"], 1)
        self.assertEqual(m["memory_recall_failed"], 1)
        self.assertEqual(m["memory_recall_ok"], 0)
        self.assertTrue(m["memory_available"], "available=True（被装配过）")
        self.assertFalse(m["memory_participated"], "participated=False（没成功召回）")

    def test_mixed_recall(self):
        """混合：2 成功 + 1 失败 → participated=True, ok=2, failed=1。"""
        events = [
            _evt("run_meta", {"memory_quality": {"retrieval_ok": True}}, "e1", 1),
            _evt("run_meta", {"memory_quality": {"retrieval_ok": False}}, "e2", 2),
            _evt("run_meta", {"memory_quality": {"retrieval_ok": True}}, "e3", 3),
        ]
        m = _memory_metrics(events)
        self.assertEqual(m["memory_recall_ok"], 2)
        self.assertEqual(m["memory_recall_failed"], 1)
        self.assertTrue(m["memory_participated"])

    def test_unrelated_run_meta_ignored(self):
        """非 memory_quality 的 run_meta 事件不计入。"""
        events = [_evt("run_meta", {"contract_snapshot": {"user_goal": "x"}})]
        m = _memory_metrics(events)
        self.assertEqual(m["memory_recall_events"], 0)

    def test_retrieval_ok_defaults_true(self):
        """retrieval_ok 缺省（旧埋点兼容）→ 视为成功。"""
        events = [_evt("run_meta", {"memory_quality": {}})]
        m = _memory_metrics(events)
        self.assertTrue(m["memory_participated"])

    def test_compute_flow_metrics_has_memory_key(self):
        """compute_flow_metrics 返回 dict 含 memory 维度（G003 集成）。"""
        detail = SimpleNamespace(
            nodes=[_node()], events=[_evt("run_meta", {"memory_quality": {"retrieval_ok": True}})],
        )
        result = compute_flow_metrics(detail)
        self.assertIn("memory", result)
        self.assertTrue(result["memory"]["memory_participated"])
        # 原有三维度不受影响
        self.assertIn("topology", result)
        self.assertIn("reliability", result)
        self.assertIn("resources", result)


if __name__ == "__main__":
    unittest.main()
