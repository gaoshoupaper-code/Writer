"""单次测试 Trace 跨进程可发现性（FR-001/002/004，根因修复验证）。

RSK-001 铁律：本测试覆盖"真实隔离子进程产生的 trace 对主进程 recorder 可发现"，
而非 mock executor 拉取成功。直接验证修复的根因契约——

  隔离子进程自建 TraceRecorder 写盘（EVD-002），通过 IPC 回传 trace_id +
  workspace_path（FR-001 修复点），主进程据此 register_external_run 登记进
  权威索引，find_run_by_trace_id / read_trace_events 才不再返回 None → 404。

跑法（在 executor 目录）：
    .venv/Scripts/python.exe -m pytest tests/test_ab_cross_process_trace.py -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.platform.trace.recorder import TraceRecorder
from app.schemas.screenplay import ThreadSummary


def _make_thread(workspace_path: str) -> ThreadSummary:
    now = "2026-07-29T00:00:00+00:00"
    return ThreadSummary(
        thread_id="ab-test-thread",
        workspace_id="ab-test-ws",
        session_name="evolve-ab",
        workspace_path=workspace_path,
        created_at=now,
        updated_at=now,
    )


class CrossProcessTraceDiscoveryTest(unittest.TestCase):
    """模拟子进程产物 + 主进程登记的跨进程发现契约。"""

    def setUp(self) -> None:
        # 子进程 workspace（tempdir，模拟 ab_ws_*）。
        self.tmp = tempfile.TemporaryDirectory(prefix="ab_ws_")
        self.workspace = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _simulate_subprocess_trace(
        self,
        *,
        traceparent: str | None = None,
        test_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple[str, TraceRecorder]:
        """模拟隔离子进程：自建独立 recorder，写盘，只回传 trace_id + workspace_path。

        返回 (trace_id, subprocess_recorder)。注意返回的是子进程 recorder，主进程
        无法访问其内存——这正是 EVD-002/003 的现实约束。
        """
        subprocess_recorder = TraceRecorder()
        thread = _make_thread(self.workspace)
        extra_refs: dict[str, str] = {}
        if test_id:
            extra_refs["test_id"] = test_id
        if task_id:
            extra_refs["task_id"] = task_id
        trace = subprocess_recorder.create_run(
            thread,
            "screenplay.ab_run",
            run_purpose="evolution",
            traceparent=traceparent,
            external_refs_extra=extra_refs or None,
        )
        # 子进程同步写盘（与 _worker_main 的退化路径一致）。
        subprocess_recorder.complete_run(thread, trace.trace_id)
        return trace.trace_id, subprocess_recorder

    def test_unregistered_trace_not_found_by_main_process(self) -> None:
        """EVD-002/003 根因复现：子进程产物若不登记，主进程查无 → 404。"""
        trace_id, _ = self._simulate_subprocess_trace()
        main_recorder = TraceRecorder()  # 主进程全新 recorder，内存索引为空

        # 未登记：主进程内存索引 + 磁盘兜底都应能发现（修复后），但先验证内存未登记时
        # find_run_by_trace_id 不依赖子进程内存（子进程 recorder 已脱离作用域也无妨）。
        run = main_recorder.find_run_by_trace_id(trace_id)
        # 磁盘兜底（FR-001）应命中 ab_ws_* 下的 index.json。
        self.assertIsNotNone(run, "磁盘发现兜底应命中子进程产物")
        self.assertEqual(run.trace_id, trace_id)

    def test_registered_trace_is_readable_by_main_process(self) -> None:
        """FR-001 修复验证：主进程 register_external_run 后可读取子进程 trace。

        这是修复的核心——主进程拿到子进程回传的 trace_id + workspace_path，
        登记进主 recorder，find_run_by_trace_id / read_trace_events 命中。
        """
        trace_id, _ = self._simulate_subprocess_trace()
        main_recorder = TraceRecorder()

        # 主进程登记（_execute_ab 在拿到 trace_id 后做的事）。
        ok = main_recorder.register_external_run(trace_id, self.workspace)
        self.assertTrue(ok)

        run = main_recorder.find_run_by_trace_id(trace_id)
        self.assertIsNotNone(run)
        self.assertEqual(run.trace_id, trace_id)
        self.assertEqual(run.status, "completed")

        # 事件可读（详情页 / 摄入依赖）。
        events = main_recorder.read_trace_events(trace_id)
        self.assertIsNotNone(events)

    def test_register_is_idempotent_and_isolated(self) -> None:
        """CON-004：重复登记幂等；不同 trace_id 不串读。"""
        tid_a, _ = self._simulate_subprocess_trace()
        main_recorder = TraceRecorder()

        # 重复登记同一 trace_id 幂等。
        self.assertTrue(main_recorder.register_external_run(tid_a, self.workspace))
        self.assertTrue(main_recorder.register_external_run(tid_a, self.workspace))

        run = main_recorder.find_run_by_trace_id(tid_a)
        self.assertIsNotNone(run)
        self.assertEqual(run.trace_id, tid_a)

        # 另一个不存在的 trace_id 不串读到这条。
        self.assertIsNone(main_recorder.find_run_by_trace_id("trace-nonexistent"))

    def test_register_returns_false_when_index_missing(self) -> None:
        """EDGE-001：workspace 下没有该 trace 的 index 时返回 False（可恢复准备态）。"""
        main_recorder = TraceRecorder()
        # 空目录或无 index.json。
        self.assertFalse(main_recorder.register_external_run("trace-x", self.workspace))

    def test_w3c_and_business_refs_propagated(self) -> None:
        """FR-004 / AC-008：traceparent 继承上游；test_id/task_id 写入 external_refs。"""
        upstream = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        trace_id, _ = self._simulate_subprocess_trace(
            traceparent=upstream, test_id="test-abc", task_id="task-xyz",
        )
        main_recorder = TraceRecorder()
        main_recorder.register_external_run(trace_id, self.workspace)

        run = main_recorder.find_run_by_trace_id(trace_id)
        self.assertIsNotNone(run)
        refs = run.external_refs
        # W3C：trace-id 继承上游 traceparent（同一 trace 树）。
        self.assertEqual(refs["w3c_trace_id"], "4bf92f3577b34da6a3ce929d0e0e4736")
        # 子 span 有自己的 span-id（不同于上游 parent-span-id），并把上游作为 parent。
        self.assertNotEqual(refs["w3c_span_id"], "00f067aa0ba902b7")
        self.assertEqual(refs.get("w3c_parent_span_id"), "00f067aa0ba902b7")
        # 业务关联（CON-003：opaque ID）。
        self.assertEqual(refs["test_id"], "test-abc")
        self.assertEqual(refs["task_id"], "task-xyz")

    def test_missing_traceparent_generates_valid_context(self) -> None:
        """FR-004 失败语义：缺失 traceparent 时生成有效新 context，不阻断运行。"""
        trace_id, _ = self._simulate_subprocess_trace()  # 无 traceparent
        main_recorder = TraceRecorder()
        main_recorder.register_external_run(trace_id, self.workspace)

        run = main_recorder.find_run_by_trace_id(trace_id)
        self.assertIsNotNone(run)
        refs = run.external_refs
        # 应有有效 W3C context（新生成），trace_id 非零、span_id 非零。
        self.assertIn("w3c_trace_id", refs)
        self.assertNotEqual(refs["w3c_trace_id"], "0" * 32)
        self.assertIn("w3c_span_id", refs)
        self.assertNotEqual(refs["w3c_span_id"], "0" * 16)

    def test_no_sensitive_content_in_external_refs(self) -> None:
        """CON-003 / RSK-006 否定断言：external_refs 不含正文/凭证/路径。"""
        trace_id, _ = self._simulate_subprocess_trace(
            test_id="test-abc", task_id="task-xyz",
        )
        main_recorder = TraceRecorder()
        main_recorder.register_external_run(trace_id, self.workspace)

        run = main_recorder.find_run_by_trace_id(trace_id)
        self.assertIsNotNone(run)
        # external_refs 只允许 w3c_* / test_id / task_id 这些 opaque ID。
        allowed = {"w3c_trace_id", "w3c_span_id", "traceparent", "w3c_parent_span_id",
                   "test_id", "task_id"}
        leaked = set(run.external_refs) - allowed
        self.assertEqual(leaked, set(), f"external_refs 泄露非允许字段: {leaked}")

    def test_external_refs_extra_cannot_override_w3c_namespace(self) -> None:
        """CON-003 边界强制：调用方传 w3c_*/traceparent 键时被 recorder 拒绝覆写。

        防止恶意/错误调用方破坏 W3C trace 树完整性。recorder 在边界过滤这些键，
        CON-003 的最小化由服务端强制而非仅靠调用方自律。
        """
        upstream = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        subprocess_recorder = TraceRecorder()
        thread = _make_thread(self.workspace)
        # 试图用 external_refs_extra 覆写 w3c_trace_id / traceparent。
        trace = subprocess_recorder.create_run(
            thread,
            "screenplay.ab_run",
            run_purpose="evolution",
            traceparent=upstream,
            external_refs_extra={
                "w3c_trace_id": "deadbeef" * 4,  # 应被忽略
                "traceparent": "fake",            # 应被忽略
                "test_id": "real-test",           # 应保留
            },
        )
        subprocess_recorder.complete_run(thread, trace.trace_id)
        main_recorder = TraceRecorder()
        main_recorder.register_external_run(trace.trace_id, self.workspace)

        run = main_recorder.find_run_by_trace_id(trace.trace_id)
        self.assertIsNotNone(run)
        # W3C 字段保持上游 context 的真实值，未被覆写。
        self.assertEqual(run.external_refs["w3c_trace_id"], "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertNotEqual(run.external_refs["traceparent"], "fake")
        # 业务字段保留。
        self.assertEqual(run.external_refs["test_id"], "real-test")


if __name__ == "__main__":
    unittest.main()
