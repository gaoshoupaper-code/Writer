"""FR-007 / DEC-006 / AC-005/006 / EDGE-004：架构层问题上报机制单测。

覆盖 report_architecture_issue 工具 + ArchitectureIssuesRepo + issue_report.md 落盘：
  - 合法上报：落盘 + 写表成功（AC-005）
  - 非法 layer：拒绝，不污染表（AC-005 / FR-007 失败语义）
  - 空 evidence_ref：拒绝（EDGE-004）
  - 上报与 propose_evolution_point 区分（AC-006）：架构问题落 architecture_issues，
    不进 evolve_points
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 独占临时 DB + payload 目录（直接赋值，避免 setdefault 在跨测试模块共享 env 时被早先
# 导入的模块污染——test_evolve_visibility_contract 等也设 EVOLUTION_DB）。
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
_tmp_payload = tempfile.mkdtemp()
os.environ["EVOLUTION_DB"] = _tmp_db.name
os.environ["EXECUTOR_URL"] = "http://127.0.0.1:0"
os.environ["TRACE_PAYLOAD_DIR"] = _tmp_payload

import app.core.db as db  # noqa: E402
from app.core.settings import settings  # noqa: E402
from app.evolve.ctx import EvolveContext, set_tool_context  # noqa: E402
from app.evolve.evolve_repo import (  # noqa: E402
    ARCHITECTURE_LAYERS,
    ArchitectureIssuesRepo,
    EvolvePointsRepo,
)


class ArchitectureIssueReportTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个测试重置到独占临时 DB，跨测试无状态泄漏。
        settings.evolution_db = _tmp_db.name
        settings.trace_payload_dir = _tmp_payload
        db._conn = None
        db.init_db()
        self.session_id = "sess-arch-test"
        # 建一个 evolve_sessions 行（子表逻辑外键）
        db.execute(
            """INSERT INTO evolve_sessions
               (session_id, case_id, status, created_at)
               VALUES (?, ?, 'running', ?)""",
            (self.session_id, "case-1", "2026-07-31T00:00:00+00:00"),
        )
        # 注入 tool context
        ctx = EvolveContext(self.session_id, "case-1")
        set_tool_context(ctx)

    def tearDown(self) -> None:
        set_tool_context(None)
        db.execute("DELETE FROM evolve_architecture_issues WHERE session_id=?", (self.session_id,))
        db.execute("DELETE FROM evolve_points WHERE session_id=?", (self.session_id,))
        db.execute("DELETE FROM evolve_sessions WHERE session_id=?", (self.session_id,))
        db._conn = None

    def _tool(self):
        from app.evolve.agent.tools.issues import make_issues_tools
        return make_issues_tools()[0]

    def test_legal_report_lands_file_and_table(self) -> None:
        """AC-005：合法上报 → issue_report.md 落盘 + architecture_issues 表新增。"""
        result = self._tool().invoke({
            "layer": "executor",
            "problem": "trace LLM 节点无输出：importer 投影前不回填 payload。",
            "evidence_ref": "EVD-001：evolution importer 投影前不回填 payload",
            "note": "根因在 evolution 端投影代码，六要素外，上报不硬修。",
        })
        self.assertIn("已上报架构层问题", result)
        rows = ArchitectureIssuesRepo.list_by_session(self.session_id)
        self.assertEqual(len(rows), 1)
        issue = rows[0]
        self.assertEqual(issue["layer"], "executor")
        self.assertEqual(issue["status"], "reported")
        self.assertIsNotNone(issue["report_path"])
        # issue_report.md 文件确实落盘
        self.assertTrue(Path(issue["report_path"]).exists())

    def test_illegal_layer_rejected_without_polluting_table(self) -> None:
        """AC-005 / FR-007 失败语义：非法 layer 拒绝，architecture_issues 表不新增。"""
        result = self._tool().invoke({
            "layer": "prompts",  # 六要素内，不该上报
            "problem": "某 prompt 有缺陷",
            "evidence_ref": "f01",
            "note": "",
        })
        self.assertIn("非法 layer", result)
        self.assertEqual(ArchitectureIssuesRepo.list_by_session(self.session_id), [])

    def test_empty_evidence_ref_rejected(self) -> None:
        """EDGE-004：evidence_ref 为空拒绝上报。"""
        result = self._tool().invoke({
            "layer": "framework",
            "problem": "deepagents write 不支持覆盖",
            "evidence_ref": "   ",
            "note": "",
        })
        self.assertIn("不能为空", result)
        self.assertEqual(ArchitectureIssuesRepo.list_by_session(self.session_id), [])

    def test_report_vs_propose_do_not_mix(self) -> None:
        """AC-006：架构问题落 architecture_issues，不进 evolve_points（两者不混用）。"""
        self._tool().invoke({
            "layer": "executor",
            "problem": "投影缺陷",
            "evidence_ref": "EVD-001",
            "note": "",
        })
        # 架构问题表有 1 条
        self.assertEqual(len(ArchitectureIssuesRepo.list_by_session(self.session_id)), 1)
        # 进化点表为空（架构上报不混进进化点）
        self.assertEqual(EvolvePointsRepo.list_by_session(self.session_id), [])

    def test_layer_enum_complete(self) -> None:
        """AC-008 配套：六要素外的归属层枚举完整覆盖需求定义。"""
        expected = {"assembly", "executor", "framework", "eval-infra", "data-pipeline"}
        self.assertEqual(set(ARCHITECTURE_LAYERS), expected)

    def test_multiple_reports_get_incrementing_seq(self) -> None:
        """同一 session 多次上报，seq 自增（前端排序依据）。"""
        for i in range(3):
            self._tool().invoke({
                "layer": "executor",
                "problem": f"问题 {i}",
                "evidence_ref": f"evidence-{i}",
                "note": "",
            })
        rows = ArchitectureIssuesRepo.list_by_session(self.session_id)
        self.assertEqual([r["seq"] for r in rows], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
