"""FR-004 evidence_ref 多源校验测试（AC-005）。

验证 write_design_doc 的多源 evidence_ref 校验：
  - AC-005：评估无内容 finding 但有契约违反 cv-id → write_design_doc 通过；
            引用不存在的 ID → 报具体哪个源；不再有"无 finding 死局短路"。

设计依据：.claude/md/20260801_192157_进化信息可见性与评估漏判.md FR-004 / EVD-007 / DEC-004
"""
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
from app.evolve.agent.tools.flow import DesignChange, make_flow_tools  # noqa: E402
from app.evolve.ctx import EvolveContext, set_tool_context  # noqa: E402


def setUpModule() -> None:
    db._conn = None
    db.init_db()


def tearDownModule() -> None:
    db._conn = None
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


def _invoke_write_design_doc(changes, eval_snapshot):
    """绑定 ctx 并调用 write_design_doc 工具，返回结果字符串。

    mock docs.write_design_doc 和 ev_db.update_session（flow.py 内部 from app.evolve import
    db as ev_db），隔离落盘逻辑，聚焦校验。
    """
    ctx = EvolveContext("sess-test")
    ctx.eval_snapshot = eval_snapshot
    set_tool_context(ctx)

    tools = make_flow_tools()
    wdd = next(t for t in tools if t.name == "write_design_doc")

    # mock 落盘层：docs.write_design_doc（模块级 from app.evolve import docs）+
    # app.evolve.db.update_session（函数内 from app.evolve import db as ev_db）
    with patch("app.evolve.docs.write_design_doc", return_value="/tmp/design_doc.md"), \
         patch("app.evolve.db.update_session", return_value=None):
        result = wdd.invoke({"changes": [c.model_dump() for c in changes], "rationale": "test"})
    return result, ctx


class EvidenceRefMultisourceTest(unittest.TestCase):
    """AC-005：evidence_ref 多源校验。"""

    def test_ac005_cv_only_passes_when_no_content_finding(self):
        """AC-005 核心场景：评估无内容 finding，但结构性 finding 暴露 cv-memory_recalled
        → 进化引用 cv-memory_recalled 通过（移除了"无 finding 死局短路"）。
        """
        eval_snapshot = {
            "findings": [
                # 只有一条结构性 finding，它自己声明 evidence_ref=[cv-memory_recalled]
                {"id": "f01", "dimension": "结构性", "evidence_ref": ["cv-memory_recalled"]},
            ],
        }
        changes = [DesignChange(
            target="middleware/memory_recall.py", change_desc="装配记忆召回",
            reason="记忆缺失", evidence_ref=["cv-memory_recalled"],
            expected_up="记忆参与", expected_down="延迟",
        )]
        result, ctx = _invoke_write_design_doc(changes, eval_snapshot)
        self.assertIn("设计文档已产出", result, f"应通过，实际：{result}")

    def test_ac005_finding_id_still_works(self):
        """向后兼容：引用内容 finding id（f01）仍通过。"""
        eval_snapshot = {"findings": [{"id": "f01", "dimension": "内容质量"}]}
        changes = [DesignChange(
            target="prompts/x.md", change_desc="改", reason="r",
            evidence_ref=["f01"], expected_up="u", expected_down="d",
        )]
        result, _ = _invoke_write_design_doc(changes, eval_snapshot)
        self.assertIn("设计文档已产出", result)

    def test_ac005_mixed_sources_pass(self):
        """引用 finding id + cv-id 混合 → 通过。"""
        eval_snapshot = {"findings": [
            {"id": "f01", "dimension": "内容质量"},
            {"id": "f02", "dimension": "结构性", "evidence_ref": ["cv-review_executed"]},
        ]}
        changes = [DesignChange(
            target="x", change_desc="c", reason="r",
            evidence_ref=["f01", "cv-review_executed"],
            expected_up="u", expected_down="d",
        )]
        result, _ = _invoke_write_design_doc(changes, eval_snapshot)
        self.assertIn("设计文档已产出", result)

    def test_ac005_nonexistent_finding_id_reports_specific_source(self):
        """引用不存在的 finding id → 报具体源（finding id 不在列表）。"""
        eval_snapshot = {"findings": [{"id": "f01"}]}
        changes = [DesignChange(
            target="x", change_desc="c", reason="r",
            evidence_ref=["f99"],  # 不存在
            expected_up="u", expected_down="d",
        )]
        result, _ = _invoke_write_design_doc(changes, eval_snapshot)
        self.assertIn("校验失败", result)
        self.assertIn("f99", result)
        self.assertIn("finding", result.lower())

    def test_ac005_nonexistent_cv_id_reports_specific_source(self):
        """引用不存在的 cv-id → 报具体源（cv-id 不在结构性 finding 暴露列表）。"""
        eval_snapshot = {"findings": [{"id": "f01"}]}  # 无结构性 finding
        changes = [DesignChange(
            target="x", change_desc="c", reason="r",
            evidence_ref=["cv-memory_recalled"],  # 但无结构性 finding 暴露它
            expected_up="u", expected_down="d",
        )]
        result, _ = _invoke_write_design_doc(changes, eval_snapshot)
        self.assertIn("校验失败", result)
        self.assertIn("cv-memory_recalled", result)
        self.assertIn("cv-id", result.lower())

    def test_ac005_no_evidence_source_returns_deadend(self):
        """所有证据源都为空 → 返回死局（不再是"无 finding 死局短路"，是"无证据源死局"）。"""
        eval_snapshot = {"findings": []}
        changes = [DesignChange(
            target="x", change_desc="c", reason="r",
            evidence_ref=["f01"],  # 但 eval_snapshot 空
            expected_up="u", expected_down="d",
        )]
        result, ctx = _invoke_write_design_doc(changes, eval_snapshot)
        self.assertIn("没有任何可引用的证据源", result)
        # emit_step 的 reason 记在 ctx.steps 里（FR-004 语义：no_evidence_source 替代旧 no_findings）

    def test_empty_evidence_ref_rejected(self):
        """空 evidence_ref → 报缺 evidence_ref。"""
        eval_snapshot = {"findings": [{"id": "f01"}]}
        changes = [DesignChange(
            target="x", change_desc="c", reason="r",
            evidence_ref=[],  # 空
            expected_up="u", expected_down="d",
        )]
        result, _ = _invoke_write_design_doc(changes, eval_snapshot)
        self.assertIn("缺少 evidence_ref", result)

    def test_deadend_message_changed_from_no_findings(self):
        """死局消息从"no_findings"改为"no_evidence_source"（FR-004 语义变更）。"""
        eval_snapshot = {"findings": []}
        changes = [DesignChange(
            target="x", change_desc="c", reason="r",
            evidence_ref=["f01"], expected_up="u", expected_down="d",
        )]
        result, _ = _invoke_write_design_doc(changes, eval_snapshot)
        # 旧消息是"评估报告没有可引用的结构化 finding"，新消息是"没有任何可引用的证据源"
        self.assertNotIn("评估报告没有可引用的结构化 finding", result)
        self.assertIn("证据源", result)


if __name__ == "__main__":
    unittest.main()
