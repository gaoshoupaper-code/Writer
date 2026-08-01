"""FR-001 契约结构性 omission 维度单元测试（AC-001 / AC-002 / AC-013）。

验证 _compute_contract_coverage_matrix 的 structural_omission 维度：
  - AC-001：契约 DSL 可表达结构性期望（memory/subagent/review 三类断言），
            违反时输出结构化违反记录（含契约 ID + 实际值 + 期望值）。
  - AC-002：契约校验确定性（3 次求值 bit-level 一致，0 LLM 调用）。
  - AC-013：契约 DSL 非图灵完备（本方案是规则求值，非 DSL 引擎，天然满足——
            测试断言无 LLM 调用 + 无循环/函数定义/exec 能力）。

设计依据：.claude/md/20260801_192157_进化信息可见性与评估漏判.md FR-001 / CON-003 / DEC-001
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["EVOLUTION_DB"] = _tmp_db.name
os.environ["EXECUTOR_URL"] = "http://127.0.0.1:0"

import app.core.db as db  # noqa: E402
from app.dossier.compiler import (  # noqa: E402
    _compute_contract_coverage_matrix,
    _structural_omission_items,
)


def setUpModule() -> None:
    db._conn = None
    db.init_db()


def tearDownModule() -> None:
    db._conn = None
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


def _full_contract(memory_required=True, review_required=True, expected_subagents=None):
    """一份完整 contract_parsed（含 structural_expectations）。"""
    return {
        "user_goal": "写玄幻", "task_type": "玄幻长篇连载",
        "promised_artifacts": [{"kind": "chapter", "desc": "正文", "required": True}],
        "hard_constraints": [{"constraint": "10章", "category": "篇幅"}],
        "style_preferences": [{"pref": "爽文", "category": "文风"}],
        "scope": "正文",
        "applicable_stages": ["interview", "storybuilding", "detail-outline", "writing"],
        "structural_expectations": {
            "memory_required": memory_required,
            "review_required": review_required,
            "expected_subagents": expected_subagents or ["interview", "storybuilding", "writing"],
        },
    }


def _healthy_facts():
    """一份满足全部结构性期望的 facts。"""
    return {
        "deliveries": {"interview": {"d.md": {}}, "storybuilding": {"c.md": {}}, "writing": {"ch.md": {}}},
        "review_chain": [{"reviewer": "review"}],
        "memory": {"memory_recall_ok": 2, "memory_participated": True},
        "reliability": {"review_calls": 1},
    }


class StructuralOmissionBasicTest(unittest.TestCase):
    """AC-001 / AC-002 / AC-013：基本场景 + 确定性 + 非图灵完备。"""

    def test_ac001_memory_missing_violation(self):
        """AC-001 标本：契约声明需要记忆，但 facts 里记忆未参与 → 输出违反记录。

        EVD-002 案例：架构蓝图有记忆系统，trace 中未出现，进化 agent 没提。
        """
        contract = _full_contract(memory_required=True)
        facts = _healthy_facts()
        # 破坏记忆参与
        facts["memory"] = {"memory_recall_ok": 0, "memory_participated": False}
        matrix = _compute_contract_coverage_matrix(contract, facts)
        mem_item = next(i for i in matrix["items"]
                        if i["dim"] == "structural_omission" and i["key"] == "memory_recalled")
        self.assertEqual(mem_item["status"], "missing")
        # 违反记录含实际值 + 期望（AC-001：契约 ID + 实际值 + 期望值）
        self.assertIn("evidence", mem_item)
        self.assertFalse(mem_item["evidence"]["memory_participated"])
        self.assertIn("reason", mem_item)

    def test_ac001_two_violations(self):
        """AC-001：记忆缺失 + review 缺失 → 2 条结构性违反记录。"""
        contract = _full_contract(memory_required=True, review_required=True)
        facts = _healthy_facts()
        facts["memory"] = {"memory_recall_ok": 0, "memory_participated": False}
        facts["review_chain"] = []
        facts["reliability"] = {"review_calls": 0}
        matrix = _compute_contract_coverage_matrix(contract, facts)
        struct_missing = [i for i in matrix["items"]
                          if i["dim"] == "structural_omission" and i["status"] == "missing"]
        missing_keys = {i["key"] for i in struct_missing}
        self.assertEqual(missing_keys, {"memory_recalled", "review_executed"})
        # complete=False（有缺失）
        self.assertFalse(matrix["complete"])

    def test_ac001_subagent_missing(self):
        """AC-001：应参与的 subagent 缺失 → 违反记录含缺失列表。"""
        contract = _full_contract(expected_subagents=["interview", "storybuilding", "writing", "detail-outline"])
        facts = _healthy_facts()  # 缺 detail-outline
        matrix = _compute_contract_coverage_matrix(contract, facts)
        sub_item = next(i for i in matrix["items"]
                        if i["dim"] == "structural_omission" and i["key"] == "subagents_complete")
        self.assertEqual(sub_item["status"], "missing")
        self.assertIn("detail-outline", sub_item["evidence"]["missing"])

    def test_ac002_deterministic_three_runs(self):
        """AC-002：同一份契约 + trace 跑 3 次，结果完全一致（bit-level）。"""
        contract = _full_contract()
        facts = _healthy_facts()
        r1 = json.dumps(_compute_contract_coverage_matrix(contract, facts), sort_keys=True)
        r2 = json.dumps(_compute_contract_coverage_matrix(contract, facts), sort_keys=True)
        r3 = json.dumps(_compute_contract_coverage_matrix(contract, facts), sort_keys=True)
        self.assertEqual(r1, r2)
        self.assertEqual(r2, r3)

    def test_ac002_ac013_no_llm_calls(self):
        """AC-002 / AC-013：契约校验过程无 LLM 调用。

        本方案是确定性规则求值（非 DSL 引擎），patch llm.chat 确保不被调用。
        """
        from app.core import llm
        with patch.object(llm, "chat", side_effect=AssertionError("契约校验不得调 LLM")):
            contract = _full_contract()
            facts = _healthy_facts()
            # _compute_contract_coverage_matrix 是纯函数，不应触发任何 llm.chat
            matrix = _compute_contract_coverage_matrix(contract, facts)
            self.assertIsNotNone(matrix)
        # _structural_omission_items 同样纯确定性
        with patch.object(llm, "chat", side_effect=AssertionError("结构性判定不得调 LLM")):
            _structural_omission_items(contract, facts)

    def test_ac013_no_turing_complete_features(self):
        """AC-013：契约 DSL 非图灵完备——禁循环/函数定义/任意执行。

        本方案结构性期望是固定字段（memory_required/review_required/expected_subagents），
        求值是布尔/成员/计数判定，没有也不接受可执行代码。这里通过断言 _structural_omission_items
        不接受可执行字符串来固化这一约束。
        """
        # 契约里塞循环/函数定义/exec——这些应被当作无效值忽略，而非执行
        malicious = _full_contract()
        malicious["structural_expectations"]["__import__"] = "__import__('os').system('rm -rf /')"
        malicious["structural_expectations"]["memory_required"] = "True if [x for x in range(10)] else False"
        facts = _healthy_facts()
        # 不应抛、不应执行任何代码——memory_required 是 truthy 字符串 → bool() → True
        items = _structural_omission_items(malicious, facts)
        mem_item = next(i for i in items if i["key"] == "memory_recalled")
        # 字符串 truthy → expected True，facts 有记忆 → covered
        self.assertEqual(mem_item["status"], "covered")

    def test_na_when_not_declared(self):
        """EDGE-001：契约未声明某期望 → na（不惩罚）。"""
        contract = _full_contract()
        # 移除所有 structural_expectations → 全 na
        contract["structural_expectations"] = {}
        facts = _healthy_facts()
        matrix = _compute_contract_coverage_matrix(contract, facts)
        struct_items = [i for i in matrix["items"] if i["dim"] == "structural_omission"]
        self.assertTrue(all(i["status"] == "na" for i in struct_items),
                        f"未声明期望应全 na，实际：{[i['status'] for i in struct_items]}")

    def test_all_covered_completes(self):
        """全部结构性期望满足 → structural_omission 维度全 covered。"""
        contract = _full_contract()
        facts = _healthy_facts()
        matrix = _compute_contract_coverage_matrix(contract, facts)
        struct_missing = [i for i in matrix["items"]
                          if i["dim"] == "structural_omission" and i["status"] == "missing"]
        self.assertEqual(struct_missing, [], "全满足时不应有结构性 missing")


class StructuralOmissionNullCasesTest(unittest.TestCase):
    """structural_expectations 各字段 null 的降级。"""

    def test_memory_required_null_is_na(self):
        """memory_required=null → na（契约盲区诚实记录）。"""
        contract = _full_contract()
        contract["structural_expectations"]["memory_required"] = None
        facts = _healthy_facts()
        matrix = _compute_contract_coverage_matrix(contract, facts)
        mem_item = next(i for i in matrix["items"]
                        if i["dim"] == "structural_omission" and i["key"] == "memory_recalled")
        self.assertEqual(mem_item["status"], "na")

    def test_memory_quality_summary_fallback(self):
        """flow_metrics.memory 缺失时，回退用 facts.memory_quality.summary.ok_count。"""
        contract = _full_contract(memory_required=True)
        facts = _healthy_facts()
        del facts["memory"]  # 没有 flow_metrics.memory
        # 但有 memory_quality.summary
        facts["memory_quality"] = {"summary": {"ok_count": 1}}
        matrix = _compute_contract_coverage_matrix(contract, facts)
        mem_item = next(i for i in matrix["items"]
                        if i["dim"] == "structural_omission" and i["key"] == "memory_recalled")
        self.assertEqual(mem_item["status"], "covered",
                         "memory_quality.summary.ok_count>=1 应兜底判定记忆已参与")


if __name__ == "__main__":
    unittest.main()
