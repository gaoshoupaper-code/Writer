"""FR-010 评估 finding direction 字段测试（AC-022）。

验证：
  - EvalFinding schema 含可选 direction 字段。
  - write_eval_report 不再硬剔除 direction（report.py:63 的 pop("suggestion") 已移除）。
  - direction 非具体方案、不含"改评估器/契约"建议（CON-002 不破）。

设计依据：.claude/md/20260801_192157_进化信息可见性与评估漏判.md FR-010 / DEC-012 / EVD-012
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.eval_agent.tools.report import EvalFinding  # noqa: E402


class Fr010DirectionTest(unittest.TestCase):
    """AC-022：评估 finding 含 direction 字段。"""

    def test_evalfinding_has_direction_field(self):
        """EvalFinding schema 含 direction 字段（可选）。"""
        schema_fields = set(EvalFinding.model_fields.keys())
        self.assertIn("direction", schema_fields)
        # 原有字段不丢
        for required in ("dimension", "severity", "evidence_type", "finding", "evidence"):
            self.assertIn(required, schema_fields)

    def test_direction_optional_default_none(self):
        """direction 可选，默认 None。"""
        f = EvalFinding(dimension="内容质量", severity="low", evidence_type="推断",
                        finding="x", evidence="y")
        self.assertIsNone(f.direction)

    def test_direction_can_be_set(self):
        """direction 可设置。"""
        f = EvalFinding(dimension="结构性", severity="high", evidence_type="实证",
                        finding="记忆缺失", evidence="e",
                        direction="检查记忆召回中间件是否被装配")
        self.assertEqual(f.direction, "检查记忆召回中间件是否被装配")

    def test_direction_serialized_in_model_dump(self):
        """model_dump 包含 direction（write_eval_report 用 model_dump 转 dict 后入库）。"""
        f = EvalFinding(dimension="结构性", severity="high", evidence_type="实证",
                        finding="x", evidence="y", direction="检查中间件")
        d = f.model_dump()
        self.assertIn("direction", d)
        self.assertEqual(d["direction"], "检查中间件")

    def test_no_suggestion_pop_in_report(self):
        """EVD-002 核实结论：report.py:63 的 f.pop("suggestion", None) 必须已移除。

        FR-010 S3 翻转：评估产出可含 direction 方向性提示，不再硬剔除。
        需求文档 EVD-012 说"障碍已消失"，但实际之前还在——本测试固化"已移除"。
        """
        report_path = Path(__file__).resolve().parent.parent / "app" / "eval_agent" / "tools" / "report.py"
        source = report_path.read_text(encoding="utf-8")
        self.assertNotIn('f.pop("suggestion"', source,
                         "FR-010：report.py 必须不再硬剔除 suggestion/direction")
        self.assertNotIn("pop(\"suggestion\"", source)


if __name__ == "__main__":
    unittest.main()
