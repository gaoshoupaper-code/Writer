"""零用户计费不变量断言（AC-017 / CON-012 / DEC-013 / RSK-011）。

四类进化工作负载（单次测试 / 证据卷宗编纂 / 评估 / 进化）必须保持零用户计费：
不创建 credit hold、不扣减余额、不写消费/退款流水。

本测试是防御性契约——断言这四类路径的源码不引用 CreditsService/CreditsMiddleware，
防止未来误接入普通创作的计费中间件。usage 只进入 Trace 与平台成本诊断，不产生
用户财务副作用。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


# 四类进化工作负载的入口文件——必须保持零用户计费。
_EVOLUTION_FREE_WORKLOAD_FILES = [
    "evolution/app/eval_agent/api.py",
    "evolution/app/evolve/api.py",
    "evolution/app/dossier/api.py",
    "evolution/app/tests/api.py",
]

# executor 端单次测试入口。
_EXECUTOR_FREE_WORKLOAD_FILES = [
    "executor/app/routers/ab_endpoint.py",
    "executor/app/routers/internal.py",
]

# 禁止在免费工作负载路径中引用的计费类/模块。
_FORBIDDEN_CREDIT_REFERENCES = {
    "CreditsService",
    "CreditsMiddleware",
    "CreditHoldRepository",
    "settle_hold",
    "create_hold",
}


class ZeroBillingInvariantTest(unittest.TestCase):
    """AC-017：四类进化工作负载始终零用户计费。"""

    def test_evolution_workloads_do_not_reference_credits(self) -> None:
        """evolution 四类入口源码不引用任何计费类（CON-012 / DEC-013）。"""
        repo_root = Path(__file__).resolve().parents[2]
        violations: list[str] = []
        for rel_path in _EVOLUTION_FREE_WORKLOAD_FILES + _EXECUTOR_FREE_WORKLOAD_FILES:
            full = repo_root / rel_path
            if not full.exists():
                continue
            source = full.read_text(encoding="utf-8")
            for ref in _FORBIDDEN_CREDIT_REFERENCES:
                if ref in source:
                    violations.append(f"{rel_path}: 引用了 {ref}")
        self.assertEqual(
            violations, [],
            "免费进化工作负载不得引用 CreditsService/CreditsMiddleware（CON-012）: "
            + "; ".join(violations),
        )

    def test_credits_module_isolated_to_normal_creation(self) -> None:
        """CreditsService 只服务普通创作，不扩散到进化工作负载目录（RSK-011）。"""
        repo_root = Path(__file__).resolve().parents[2]
        credits_dir = repo_root / "executor" / "app" / "platform" / "credits"
        self.assertTrue(credits_dir.exists(), "credits 模块应存在（普通创作计费）")

        # 确认 evolution 目录不包含 credits 子模块。
        evolution_credits = repo_root / "evolution" / "app" / "credits"
        self.assertFalse(
            evolution_credits.exists(),
            "evolution 不应有独立 credits 模块（进化工作负载免费）",
        )

    def test_cancel_contract_defines_zero_billing_semantics(self) -> None:
        """cancel_state 契约不引入计费概念——取消成本归平台，用户零副作用（DEC-011）。"""
        from contracts.cancel_state import CancelState

        # 取消状态机只有执行状态，没有计费状态——计费不属于取消契约。
        states = {s.value for s in CancelState}
        for s in states:
            self.assertNotIn("charge", s)
            self.assertNotIn("bill", s)
            self.assertNotIn("hold", s)


if __name__ == "__main__":
    unittest.main()
