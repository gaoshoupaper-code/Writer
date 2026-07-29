"""父进程接管 canonical Trace 取消收尾（FR-002/006, EVD-006/007 根因修复验证）。

RSK-002/004 铁律：本测试覆盖"隔离子进程被强杀后，canonical Trace 不再永久
running/incomplete、无 manifest、无 run_cancelled"——直接验证修复的根因契约。

场景复现线上样本（EVD-006 trace-02c7659e…）：
  - 子进程创建 trace，写了一串事件（如 llm_start），但在终态前被 SIGKILL。
  - 子进程的 _finalize_run 永远不会跑 → trace 永久 running/incomplete、无 manifest。
  - 父进程 seal_external_cancel 接管：补 run_cancelled + 生成 manifest + 收敛 integrity +
    notify evolution。

跑法（在 executor 目录）：
    .venv/Scripts/python.exe -m pytest tests/test_executor_parent_seal.py -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.platform.trace.recorder import TraceRecorder
from app.schemas.screenplay import ThreadSummary
from contracts.trace import CancelAudit


def _make_thread(workspace_path: str) -> ThreadSummary:
    now = "2026-07-29T00:00:00+00:00"
    return ThreadSummary(
        thread_id="parent-seal-thread",
        workspace_id="parent-seal-ws",
        session_name="evolve-ab",
        workspace_path=workspace_path,
        created_at=now,
        updated_at=now,
    )


class ParentSealExternalCancelTest(unittest.TestCase):
    """父进程接管子进程 canonical Trace 收尾的根因修复验证。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ab_ws_")
        self.workspace = self.tmp.name
        # 子进程 recorder（模拟被强杀的隔离子进程）。
        self.child = TraceRecorder()
        # 父进程 recorder（接管者）。
        self.parent = TraceRecorder()
        self.thread = _make_thread(self.workspace)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _simulate_killed_subprocess(self) -> str:
        """模拟隔离子进程被强杀：创建 trace + 写事件，但不调 complete/cancel（终态前被杀）。

        返回 trace_id。子进程内存随后"消失"（我们不再用它），只剩磁盘 jsonl + index。
        """
        handle = self.child.create_run(
            self.thread,
            "screenplay.ab_run",
            run_purpose="evolution",
            external_refs_extra={"test_id": "test-1", "task_id": "task-1"},
        )
        # 写若干事件（模拟运行中的 LLM 调用）——最后一条是未配对的 llm_start（被中断中）。
        self.child.append_event(
            handle.trace_id,
            {"type": "llm_start", "status": "running", "source": "runtime",
             "input": {"messages": [{"role": "user", "content": "hi"}]}},
        )
        self.child.append_event(
            handle.trace_id,
            {"type": "llm_end", "status": "running", "source": "runtime",
             "output": {"content": "hello"}, "usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        self.child.append_event(
            handle.trace_id,
            {"type": "llm_start", "status": "running", "source": "runtime",
             "input": {"messages": [{"role": "user", "content": "more"}]}},
        )
        # 关键：不调 complete_run/cancel_run——子进程在此刻被 SIGKILL。
        return handle.trace_id

    def test_seal_after_hardkill_converges_to_cancelled_with_manifest(self) -> None:
        """EVD-006 根因修复：强杀后父进程接管 → cancelled + manifest + run_cancelled。"""
        trace_id = self._simulate_killed_subprocess()
        # 父进程登记子进程产物（与 _execute_ab 的 register_external_run 一致）。
        self.parent.register_external_run(trace_id, self.workspace)

        # 强杀前：trace 是 running/pending，无 manifest。
        run_before = self.parent.find_run_by_trace_id(trace_id)
        self.assertIsNotNone(run_before)
        self.assertEqual(run_before.status, "running")
        self.assertEqual(run_before.integrity_status, "pending")
        self.assertEqual(run_before.trace_phase, "recording")
        self.assertIsNone(run_before.manifest)

        audit = CancelAudit(cancel_id="cancel-abc", requested_by="user", reason="user_stop")
        ok = self.parent.seal_external_cancel(trace_id, cancel_audit=audit, timeout=False)

        self.assertTrue(ok)
        run_after = self.parent.find_run_by_trace_id(trace_id)
        self.assertEqual(run_after.status, "cancelled")
        self.assertEqual(run_after.integrity_status, "verified")
        self.assertEqual(run_after.trace_phase, "sealed")
        self.assertIsNotNone(run_after.manifest)
        # run_cancelled 事件必须存在（manifest 的终态锚点）。
        events = self.parent.read_trace_events(trace_id)
        self.assertTrue(any(e.type == "run_cancelled" for e in events))
        # cancel_audit 写进 index。
        self.assertEqual(run_after.cancel_audit.cancel_id, "cancel-abc")

    def test_seal_uses_disk_high_water_not_stale_event_count(self) -> None:
        """EVD-008 根因修复：父进程接管用磁盘真实高水位，不用陈旧 index event_count。

        index 的 event_count 只在终态更新，运行中保持 0；若信任它，append_event 会从
        sequence 1 续写，污染事件序列。本测试验证 manifest 的 final_sequence 正确反映
        磁盘上已写入的事件数（含接管补写的 run_cancelled）。
        """
        trace_id = self._simulate_killed_subprocess()
        self.parent.register_external_run(trace_id, self.workspace)

        # 运行中 index 的 event_count 是 0（create_run 初始值，未到终态更新）。
        idx_path = Path(self.workspace) / "traces" / "index.json"
        with idx_path.open() as f:
            idx = json.load(f)
        self.assertEqual(idx[trace_id]["event_count"], 0)

        audit = CancelAudit(cancel_id="cancel-hw")
        self.parent.seal_external_cancel(trace_id, cancel_audit=audit, timeout=False)

        run = self.parent.find_run_by_trace_id(trace_id)
        events = self.parent.read_trace_events(trace_id)
        # 磁盘上有 4 条原事件（run_start + 3 条 llm）+ 1 条补写 run_cancelled = 5。
        self.assertEqual(len(events), 5)
        self.assertEqual(run.manifest.final_sequence, 5)
        # 封存后 index 的 event_count 已被 _finalize_run 更新为真实高水位。
        with idx_path.open() as f:
            idx2 = json.load(f)
        self.assertEqual(idx2[trace_id]["event_count"], 5)

    def test_seal_timeout_writes_cancel_timeout_not_cancelled(self) -> None:
        """EDGE-007：进程边界不可确认时写 cancel_timeout 诚实告警，不谎报 cancelled。"""
        trace_id = self._simulate_killed_subprocess()
        self.parent.register_external_run(trace_id, self.workspace)
        audit = CancelAudit(cancel_id="cancel-to")
        self.parent.seal_external_cancel(trace_id, cancel_audit=audit, timeout=True)

        run = self.parent.find_run_by_trace_id(trace_id)
        self.assertEqual(run.status, "cancel_timeout")
        events = self.parent.read_trace_events(trace_id)
        self.assertTrue(any(e.type == "cancel_timeout" for e in events))
        self.assertFalse(any(e.type == "run_cancelled" for e in events))

    def test_seal_does_not_overwrite_already_completed(self) -> None:
        """CON-003/EDGE-001 单调保护：子进程 SIGKILL 前已写 completed，父进程不反改 cancelled。"""
        handle = self.child.create_run(self.thread, "screenplay.ab_run", run_purpose="evolution")
        self.child.complete_run(self.thread, handle.trace_id)
        trace_id = handle.trace_id
        self.parent.register_external_run(trace_id, self.workspace)

        audit = CancelAudit(cancel_id="cancel-late")
        ok = self.parent.seal_external_cancel(trace_id, cancel_audit=audit, timeout=False)

        # 已终态 completed，接管返回 False，状态不变。
        self.assertFalse(ok)
        run = self.parent.find_run_by_trace_id(trace_id)
        self.assertEqual(run.status, "completed")

    def test_seal_is_idempotent(self) -> None:
        """重复接管幂等：第二次 seal 不重复写 run_cancelled、不破坏 manifest。"""
        trace_id = self._simulate_killed_subprocess()
        self.parent.register_external_run(trace_id, self.workspace)
        audit = CancelAudit(cancel_id="cancel-idem")
        self.parent.seal_external_cancel(trace_id, cancel_audit=audit, timeout=False)
        events_after_first = self.parent.read_trace_events(trace_id)

        # 第二次（已是 cancelled 终态）：单调保护返回 False，无副作用。
        ok = self.parent.seal_external_cancel(trace_id, cancel_audit=audit, timeout=False)
        self.assertFalse(ok)
        events_after_second = self.parent.read_trace_events(trace_id)
        self.assertEqual(len(events_after_first), len(events_after_second))

    def test_seal_notify_evolution_called(self) -> None:
        """父进程接管封存后必须 notify evolution（否则 evolution 兜底扫描才补，延迟大）。"""
        trace_id = self._simulate_killed_subprocess()
        self.parent.register_external_run(trace_id, self.workspace)
        audit = CancelAudit(cancel_id="cancel-notify")
        with patch("app.platform.trace.recorder._notify_evolution") as mock_notify:
            self.parent.seal_external_cancel(trace_id, cancel_audit=audit, timeout=False)
            self.assertTrue(mock_notify.called)
            # 通知的是 cancelled 终态。
            args = mock_notify.call_args.args
            self.assertEqual(args[2], "cancelled")

    def test_record_cancel_requested_is_idempotent_by_cancel_id(self) -> None:
        """FR-006：同一 cancel_id 重复 record_cancel_requested 不产生第二条事件。"""
        handle = self.child.create_run(self.thread, "screenplay.ab_run", run_purpose="evolution")
        trace_id = handle.trace_id
        audit = CancelAudit(cancel_id="cancel-req", requested_by="user", reason="user_stop")
        self.child.record_cancel_requested(self.thread, trace_id, audit)
        self.child.record_cancel_requested(self.thread, trace_id, audit)  # 重复
        events = self.child.read_trace_events(trace_id)
        cancel_req = [e for e in events if e.type == "cancel_requested"]
        self.assertEqual(len(cancel_req), 1)


if __name__ == "__main__":
    unittest.main()
