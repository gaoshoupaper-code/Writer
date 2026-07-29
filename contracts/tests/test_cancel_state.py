"""统一取消状态契约测试（FR-006/007, CON-003, DEC-002/005）。"""

from __future__ import annotations

import unittest

from contracts.cancel_state import (
    CANCEL_TERMINAL_STATES,
    HARD_STOP_DEADLINE_SECONDS,
    TERMINAL_STATES,
    CancelState,
    can_transition_to,
    canonical_status,
    is_cancel_terminal,
    is_terminal,
)


class CancelStateContractTest(unittest.TestCase):
    def test_state_machine_values(self) -> None:
        """状态字典覆盖取消全生命周期（FR-006, DEC-002/008）。"""
        self.assertEqual(CancelState.PENDING.value, "pending")
        self.assertEqual(CancelState.RUNNING.value, "running")
        self.assertEqual(CancelState.CANCELLING.value, "cancelling")
        self.assertEqual(CancelState.CANCELLED.value, "cancelled")
        self.assertEqual(CancelState.CANCEL_TIMEOUT.value, "cancel_timeout")

    def test_hard_stop_deadline_is_ten_seconds(self) -> None:
        """NFR-001 / DEC-005：十秒硬终止时限。"""
        self.assertEqual(HARD_STOP_DEADLINE_SECONDS, 10.0)

    def test_terminal_states_are_monotonic(self) -> None:
        """CON-003：终态集合包含所有不可逆状态。"""
        for state in ("completed", "done", "failed", "cancelled", "cancel_timeout"):
            self.assertIn(state, TERMINAL_STATES)
            self.assertTrue(is_terminal(state))

    def test_cancel_terminals_excluded_from_failure(self) -> None:
        """CON-003：取消类终态与失败分开（cancelled 不算 failed）。"""
        self.assertIn("cancelled", CANCEL_TERMINAL_STATES)
        self.assertIn("cancel_timeout", CANCEL_TERMINAL_STATES)
        self.assertNotIn("failed", CANCEL_TERMINAL_STATES)
        self.assertTrue(is_cancel_terminal("cancelled"))
        self.assertFalse(is_cancel_terminal("failed"))


class MonotonicTransitionTest(unittest.TestCase):
    """CON-003 / EDGE-002：终态单调性规则。"""

    def test_non_terminal_can_transition_freely(self) -> None:
        self.assertTrue(can_transition_to("running", "cancelling"))
        self.assertTrue(can_transition_to("cancelling", "cancelled"))
        self.assertTrue(can_transition_to(None, "running"))

    def test_terminal_to_same_is_idempotent(self) -> None:
        """幂等：重复停止返回同一终态。"""
        for state in ("cancelled", "completed", "failed"):
            self.assertTrue(can_transition_to(state, state))

    def test_cancelled_not_overwritten_by_failed(self) -> None:
        """EVD-006 根因：cancelled 不得被摄入映射为 failed。"""
        self.assertFalse(can_transition_to("cancelled", "failed"))
        self.assertFalse(can_transition_to("cancelled", "done"))
        self.assertFalse(can_transition_to("cancelled", "completed"))

    def test_done_not_overwritten_by_cancelled(self) -> None:
        """先完成后到达的停止返回已终结，不反改 cancelled（EDGE-002）。"""
        self.assertFalse(can_transition_to("done", "cancelled"))
        self.assertFalse(can_transition_to("completed", "cancelled"))

    def test_cancel_timeout_recoverable_to_cancelled(self) -> None:
        """EDGE-004 恢复路径：cancel_timeout 后台确认终止可转 cancelled。"""
        self.assertTrue(can_transition_to("cancel_timeout", "cancelled"))
        # 但不能转成其他终态
        self.assertFalse(can_transition_to("cancel_timeout", "completed"))
        self.assertFalse(can_transition_to("cancel_timeout", "failed"))


class CanonicalStatusTest(unittest.TestCase):
    """FR-007：对外统一状态字典（done/completed 内部保留，对外统一）。"""

    def test_done_canonicalizes_to_completed(self) -> None:
        self.assertEqual(canonical_status("done"), "completed")
        self.assertEqual(canonical_status("completed"), "completed")

    def test_cancel_states_preserved(self) -> None:
        self.assertEqual(canonical_status("cancelling"), "cancelling")
        self.assertEqual(canonical_status("cancelled"), "cancelled")
        self.assertEqual(canonical_status("cancel_timeout"), "cancel_timeout")

    def test_none_is_unknown(self) -> None:
        self.assertEqual(canonical_status(None), "unknown")

    def test_new_intermediate_states_not_canonicalized_to_success(self) -> None:
        """CON-009：旧客户端遇到 cancelling/cancel_timeout/pending 不得被映射成成功态。"""
        for raw in ("cancelling", "cancel_timeout", "pending"):
            canonical = canonical_status(raw)
            self.assertNotIn(canonical, ("completed", "done"))
            self.assertEqual(canonical, raw)  # 新态原样透传，旧端按字面得到非成功


class FourDimensionalContractTest(unittest.TestCase):
    """DEC-008 四维正交契约 + 取消身份持久性（FR-006/008, CON-007）。"""

    def test_pending_can_cancel_before_worker_registered(self) -> None:
        """EDGE-005：pending（执行尚未登记）即可被取消请求命中。"""
        self.assertTrue(can_transition_to("pending", "cancelling"))
        self.assertTrue(can_transition_to("pending", "cancelled"))

    def test_cancel_timeout_only_recovers_to_cancelled(self) -> None:
        """cancel_timeout 是可恢复告警态，仅可单调前进为 cancelled（EDGE-004/007）。"""
        self.assertTrue(can_transition_to("cancel_timeout", "cancelled"))
        # 不能被改写成完成或失败
        self.assertFalse(can_transition_to("cancel_timeout", "completed"))
        self.assertFalse(can_transition_to("cancel_timeout", "failed"))
        self.assertFalse(can_transition_to("cancel_timeout", "done"))

    def test_cancelling_is_not_terminal(self) -> None:
        """cancelling 是收敛中间态，不在终态集合里（持续观察到真实终态）。"""
        self.assertFalse(is_terminal("cancelling"))
        self.assertFalse(is_terminal("pending"))

    def test_pending_integrity_not_treated_as_broken(self) -> None:
        """FR-008/AC-010：记录/封存中的 pending 完整性既非完整也非损坏。"""
        from contracts.trace import TraceRunSummary

        recording = TraceRunSummary(
            trace_id="t", workspace_id="w", thread_id="th", session_name="s",
            workspace_path="/tmp", endpoint="ab", status="running", started_at="2026",
            path="traces/t.jsonl",
            integrity_status="pending", trace_phase="recording",
        )
        # pending 不触发下游门禁（trace_incomplete=False），但也不是 verified
        self.assertFalse(recording.trace_incomplete)
        self.assertNotEqual(recording.integrity_status, "verified")

        sealed_ok = recording.model_copy(
            update={"integrity_status": "verified", "trace_phase": "sealed"}
        )
        sealed_gap = recording.model_copy(
            update={"integrity_status": "incomplete", "trace_phase": "sealed"}
        )
        self.assertFalse(sealed_ok.trace_incomplete)
        self.assertTrue(sealed_gap.trace_incomplete)

    def test_cancel_audit_carries_stable_identity(self) -> None:
        """FR-006：一次取消对应稳定幂等 cancel_id，贯穿 trace/审计/下游。"""
        from contracts.trace import CancelAudit

        audit = CancelAudit(cancel_id="cancel-abc", reason="user_stop")
        self.assertEqual(audit.cancel_id, "cancel-abc")
        # 收敛前字段可空，收敛后回填
        self.assertIsNone(audit.converge_status)
        converged = audit.model_copy(
            update={"converge_status": "cancelled", "converged_at": "2026-07-29T00:00:00Z"}
        )
        self.assertEqual(converged.converge_status, "cancelled")

    def test_run_summary_new_fields_backward_compatible(self) -> None:
        """CON-009：旧索引/旧 JSONL 不带新字段时仍可反序列化（默认值）。"""
        from contracts.trace import TraceRunSummary

        legacy = TraceRunSummary(
            trace_id="t", workspace_id="w", thread_id="th", session_name="s",
            workspace_path="/tmp", endpoint="ab", status="completed", started_at="2026",
            path="traces/t.jsonl",
        )
        # 新字段缺省：phase=None, cancel_audit=None, revision=0, integrity=legacy
        self.assertIsNone(legacy.trace_phase)
        self.assertIsNone(legacy.cancel_audit)
        self.assertEqual(legacy.lifecycle_revision, 0)
        self.assertEqual(legacy.integrity_status, "legacy")
        self.assertTrue(legacy.trace_incomplete)  # legacy 不可消费


if __name__ == "__main__":
    unittest.main()
