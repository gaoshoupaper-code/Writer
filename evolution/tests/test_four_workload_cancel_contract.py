"""四类工作负载统一取消契约测试（FR-006/010, CON-003, DEC-005/008, EDGE-007）。

验证 Phase 4 根因对齐：四类工作负载的取消收敛遵守同一契约——
  - 协作取消在 10s 内退出 → cancelled（真实停止确认）。
  - 超时未退出 → cancel_timeout（诚实告警，不谎报 cancelled，CON-003/EDGE-007）。

聚焦 eval/evolve 的 _converge_*_cancel 超时检测逻辑（dossier 的 cancel_timeout
对齐在 stop_compile 路径，单次测试在 executor ab_stop 路径，各自有专门测试）。

跑法（在 evolution 目录）：
    python -m pytest tests/test_four_workload_cancel_contract.py -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class EvalCancelConvergeTest(unittest.TestCase):
    """评估取消收敛：超时检测 → cancelled vs cancel_timeout（CON-003/EDGE-007）。"""

    def test_eval_converge_in_time_marks_cancelled(self) -> None:
        """task 在 10s 内协作退出 → cancelled（真实停止确认）。"""
        from app.eval_agent import api as eval_api

        async def run():
            # 模拟 task 立即完成（协作取消成功）。
            task = asyncio.ensure_future(asyncio.sleep(0))
            with patch.object(eval_api, "get_recorder", return_value=MagicMock(
                get_trace_id_by_session=MagicMock(return_value=None)
            )), patch.object(eval_api, "eval_repo") as mock_repo:
                await eval_api._converge_eval_cancel("eval-1", task)
                # 最终状态应为 cancelled（时限内退出）。
                mock_repo.update_session.assert_called_once_with("eval-1", status="cancelled")

        asyncio.run(run())

    def test_eval_converge_timeout_marks_cancel_timeout(self) -> None:
        """task 超时未退出 → cancel_timeout（不谎报 cancelled，EDGE-007）。

        用 mock task（done() 恒 False）模拟"卡在 C 层阻塞、deadline 后仍未退出"。
        生产收敛逻辑以 task.done() 为真实停止判据（CON-003），mock 直接控制该信号。
        """
        from app.eval_agent import api as eval_api

        async def run():
            task = MagicMock()
            task.done.return_value = False  # 模拟不可中断阻塞，deadline 后仍未退出
            task.cancel = MagicMock()
            with patch.object(eval_api, "HARD_STOP_DEADLINE_SECONDS", 0.01), \
                 patch.object(eval_api, "asyncio") as mock_aio, \
                 patch.object(eval_api, "get_recorder", return_value=MagicMock(
                     get_trace_id_by_session=MagicMock(return_value=None)
                 )), \
                 patch.object(eval_api, "eval_repo") as mock_repo:
                # 用真实 asyncio 的 wait_for/shield，但让它们 await 一个立刻完成的协程，
                # 避免 mock task 被传入 shield（shield 需要 awaitable）。
                async def _noop():
                    return None
                mock_aio.wait_for = lambda *a, **k: _noop()
                mock_aio.shield = lambda x: x
                await eval_api._converge_eval_cancel("eval-1", task)
                mock_repo.update_session.assert_called_once_with("eval-1", status="cancel_timeout")

        asyncio.run(run())


class EvolveCancelConvergeTest(unittest.TestCase):
    """进化取消收敛：超时检测 → cancelled vs cancel_timeout（CON-003/EDGE-007）。"""

    def test_evolve_converge_in_time_marks_cancelled(self) -> None:
        from app.evolve import api as evolve_api

        async def run():
            task = asyncio.ensure_future(asyncio.sleep(0))
            with patch.object(evolve_api, "get_recorder", return_value=MagicMock(
                get_trace_id_by_session=MagicMock(return_value=None)
            )), patch.object(evolve_api, "ev_db") as mock_db:
                await evolve_api._converge_evolve_cancel("sess-1", task)
                mock_db.update_session.assert_called_once_with("sess-1", status="cancelled")

        asyncio.run(run())

    def test_evolve_converge_timeout_marks_cancel_timeout(self) -> None:
        """task 超时未退出 → cancel_timeout（不谎报 cancelled，EDGE-007）。"""
        from app.evolve import api as evolve_api

        async def run():
            task = MagicMock()
            task.done.return_value = False  # 模拟不可中断阻塞，deadline 后仍未退出
            task.cancel = MagicMock()
            with patch.object(evolve_api, "HARD_STOP_DEADLINE_SECONDS", 0.01), \
                 patch.object(evolve_api, "asyncio") as mock_aio, \
                 patch.object(evolve_api, "get_recorder", return_value=MagicMock(
                     get_trace_id_by_session=MagicMock(return_value=None)
                 )), \
                 patch.object(evolve_api, "ev_db") as mock_db:
                async def _noop():
                    return None
                mock_aio.wait_for = lambda *a, **k: _noop()
                mock_aio.shield = lambda x: x
                await evolve_api._converge_evolve_cancel("sess-1", task)
                mock_db.update_session.assert_called_once_with("sess-1", status="cancel_timeout")

        asyncio.run(run())

    def test_evolve_cancel_timeout_is_terminal(self) -> None:
        """cancel_timeout 必须在 evolve 的终态集合里（轮询才会停，不永久 running）。"""
        from app.evolve.ctx import TERMINAL_STATUSES
        self.assertIn("cancel_timeout", TERMINAL_STATUSES)


class ContractAlignmentTest(unittest.TestCase):
    """四类工作负载取消契约统一性（DEC-005/008）。"""

    def test_cancel_timeout_recognized_as_terminal_everywhere(self) -> None:
        """CON-009：cancel_timeout 在共享契约与各 workload 终态集合中都被识别为终态。"""
        from contracts.cancel_state import TERMINAL_STATES, is_terminal
        from app.evolve.ctx import TERMINAL_STATUSES as evolve_terminals

        # 共享契约（executor ab_stop、evolution test poll 都用这个）
        self.assertIn("cancel_timeout", TERMINAL_STATES)
        self.assertTrue(is_terminal("cancel_timeout"))
        # evolve workload 终态集合
        self.assertIn("cancel_timeout", evolve_terminals)


if __name__ == "__main__":
    unittest.main()
