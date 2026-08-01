"""FR-002 结构性维度 finding 单元测试（AC-003 / AC-016）。

验证 build_structural_findings：
  - AC-003：结构性缺失即生成 finding（即使内容维度全绿）。
  - AC-016：结构性维度确定性不调 LLM；冲突时契约违反优先于内容维度全绿。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.eval_agent.structural import build_structural_findings, structural_violation_id  # noqa: E402


class BuildStructuralFindingsTest(unittest.TestCase):

    def _matrix_with_memory_missing(self):
        return {"items": [
            {"dim": "structural_omission", "key": "memory_recalled", "status": "missing",
             "reason": "记忆系统未参与", "evidence": {"memory_participated": False}},
            {"dim": "structural_omission", "key": "review_executed", "status": "covered",
             "reason": "review 执行 1 次", "evidence": {"review_calls": 1}},
            {"dim": "structural_omission", "key": "subagents_complete", "status": "na",
             "reason": "未声明", "evidence": None},
        ]}

    def test_ac003_missing_produces_finding_even_if_content_green(self):
        """AC-003：内容维度全绿（这里不传内容 finding），但结构性缺失 → 仍产出 finding。"""
        findings = build_structural_findings(self._matrix_with_memory_missing(), {})
        # 只 memory_recalled 是 missing → 1 条 finding
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["dimension"], "结构性")
        self.assertEqual(f["severity"], "high")  # DEC-008：缺失性缺陷一律 high
        self.assertEqual(f["evidence_type"], "实证")
        self.assertIn("记忆", f["finding"])
        self.assertEqual(f["evidence_ref"], ["cv-memory_recalled"])
        self.assertEqual(f["source_class"], "sealed")
        self.assertIsNotNone(f.get("direction"), "FR-010：结构性 finding 应附 direction")
        self.assertIn("FM-2.4", f["direction"])

    def test_ac003_no_missing_returns_empty(self):
        """结构性维度全绿 → 无 finding（正常）。"""
        matrix = {"items": [
            {"dim": "structural_omission", "key": "memory_recalled", "status": "covered",
             "reason": "ok", "evidence": {}},
        ]}
        self.assertEqual(build_structural_findings(matrix, {}), [])

    def test_ac016_no_llm_calls(self):
        """AC-016：结构性 finding 生成不调 LLM（确定性）。"""
        from app.core import llm
        with patch.object(llm, "chat", side_effect=AssertionError("结构性 finding 不得调 LLM")):
            findings = build_structural_findings(self._matrix_with_memory_missing(), {})
            self.assertEqual(len(findings), 1)

    def test_ac016_contract_priority_over_content_green(self):
        """AC-016 / DEC-008：契约违反优先——即便有内容 finding（模拟全绿场景），
        只要契约有 missing，仍产出结构性 finding（不被内容维度覆盖）。

        这里直接断言：build_structural_findings 只看 structural_omission 的 status，
        完全不接收/消费内容维度信息，天然满足"契约优先"。
        """
        matrix = self._matrix_with_memory_missing()
        # 传入的 facts 模拟"内容全绿"（无任何 badcase 信号）——但 build_structural_findings 不看内容
        facts = {"scores": {"badcase": {}}}
        findings = build_structural_findings(matrix, facts)
        self.assertEqual(len(findings), 1, "契约 missing 不被内容全绿覆盖")

    def test_empty_matrix_returns_empty(self):
        """契约矩阵为空/None → 返回空（契约未跑）。"""
        self.assertEqual(build_structural_findings(None, {}), [])
        self.assertEqual(build_structural_findings({}, {}), [])
        self.assertEqual(build_structural_findings({"items": []}, {}), [])

    def test_cv_id_stable_by_key(self):
        """cv-ID 按 key 派生（稳定，不依赖顺序）。"""
        self.assertEqual(structural_violation_id("memory_recalled"), "cv-memory_recalled")
        self.assertEqual(structural_violation_id("review_executed"), "cv-review_executed")
        # 多次调用一致
        self.assertEqual(structural_violation_id("x"), structural_violation_id("x"))

    def test_only_structural_omission_dim_processed(self):
        """只处理 dim=structural_omission 的项，其他维度（promised_artifact 等）不产出 finding。"""
        matrix = {"items": [
            {"dim": "structural_omission", "key": "memory_recalled", "status": "missing",
             "reason": "x", "evidence": None},
            {"dim": "promised_artifact", "key": "chapter:正文", "status": "missing",
             "reason": "产物缺", "evidence": None},
            {"dim": "applicable_stage", "key": "writing", "status": "missing",
             "reason": "阶段缺", "evidence": None},
        ]}
        findings = build_structural_findings(matrix, {})
        self.assertEqual(len(findings), 1, "只 1 条结构性 finding，promised_artifact/applicable_stage 不转")
        self.assertEqual(findings[0]["evidence_ref"], ["cv-memory_recalled"])


if __name__ == "__main__":
    unittest.main()
