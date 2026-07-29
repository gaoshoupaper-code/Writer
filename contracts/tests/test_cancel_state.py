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
        """状态字典覆盖取消全生命周期（FR-006, DEC-002）。"""
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


if __name__ == "__main__":
    unittest.main()
