"""评估可见性隔离契约测试（2026-07-27，证据分级可见性重构阶段 C）。

阶段 C 核心：评估 Agent 只读证据卷宗，不再直读原始 trace/工作区。
本测试固化切断后的不变量，防止后续阶段回退（需求 R1）：

  1. 工具集不含任何 read_trace* 工具（旁路硬切断）
  2. write_eval_report 不调用 load_trace_detail（flow_metrics 从卷宗读）
  3. content 评估不调 extract_deliveries（从卷宗冻结正文读）
  4. 评估启动按 dossier_id；无完整卷宗（partial/failed/不存在）一律拒（§29）
  5. 评估成功 = 封存评估卷宗（completed + sealed_dossier_id）；封存失败 = failed（R9）
  6. 评估卷宗完整性：finding 缺 evidence_ref 则封存失败（§30）
  7. 单卷宗单活动任务复用（§40）

设计依据：.claude/md/20260727_174943_进化证据分级可见性重构.md
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["EVOLUTION_DB"] = _tmp_db.name
os.environ["EXECUTOR_URL"] = "http://127.0.0.1:0"

import app.core.db as db  # noqa: E402
from app.core.settings import settings  # noqa: E402


def setUpModule() -> None:
    db._conn = None
    db.init_db()


def tearDownModule() -> None:
    db._conn = None
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


class EvalToolsetVisibilityTest(unittest.TestCase):
    """需求 R1：评估 Agent 工具集不含任何 trace 直读旁路。"""

    def test_no_trace_read_tools_in_eval_toolset(self):
        from app.eval_agent.tools import make_eval_tools
        tools = make_eval_tools()
        names = [t.name for t in tools]
        # 工具集应为 4 个：read_evidence_pack / drill_evidence / get_content_score / write_eval_report
        self.assertEqual(len(tools), 4)
        for n in names:
            self.assertNotIn("read_trace", n, f"工具集含 trace 直读旁路: {n}")
        self.assertIn("read_evidence_pack", names)
        self.assertIn("write_eval_report", names)

    def test_trace_tool_module_removed(self):
        """trace.py 已删除，import 应失败。"""
        with self.assertRaises(ImportError):
            import app.eval_agent.tools.trace  # noqa: F401


class EvalReportNoTraceBypassTest(unittest.TestCase):
    """需求 R1：write_eval_report 不调用 load_trace_detail（用 AST 检查实际调用，非注释）。"""

    def test_report_does_not_import_or_call_trace_bypass(self):
        """report.py 不应 import load_trace_detail / compute_flow_metrics（旁路切断）。

        用 AST 检查实际 import 和调用，不受注释/docstring 文字干扰。
        """
        import ast
        import inspect
        from app.eval_agent.tools import report
        tree = ast.parse(inspect.getsource(report))

        imported_names: set[str] = set()
        called_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name)
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called_names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called_names.add(func.attr)

        self.assertNotIn("load_trace_detail", imported_names,
                         "report.py import 了 load_trace_detail（trace 直读旁路）")
        self.assertNotIn("load_trace_detail", called_names,
                         "report.py 调用了 load_trace_detail（trace 直读旁路）")
        self.assertNotIn("compute_flow_metrics", imported_names,
                         "report.py import 了 compute_flow_metrics")

    def test_content_does_not_call_extract_deliveries(self):
        """content.py 不应调用 extract_deliveries（从卷宗冻结正文读）。"""
        import ast
        import inspect
        from app.eval_agent.tools import content
        tree = ast.parse(inspect.getsource(content))
        # 收集所有被引用的名字（import / 调用 / 属性访问 / 参数传递）
        referenced: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    referenced.add(alias.asname or alias.name)
        self.assertNotIn("extract_deliveries", referenced,
                         "content.py 引用了 extract_deliveries（工作区旁路未切断）")
        self.assertIn("evaluate_from_facts", referenced,
                      "content.py 应改用 evaluate_from_facts（从卷宗评估）")


class EvalStartDossierGateTest(unittest.TestCase):
    """需求 §29：评估启动按 dossier_id，只接受完整卷宗。"""

    def setUp(self):
        db.execute(
            "INSERT OR IGNORE INTO runs(trace_id, workspace_id, status, owner_user_id, "
            "run_purpose, ingested_at) VALUES(?,?,?,?,?,?)",
            ("trace-c", "ws", "completed", "u1", "user_generation", "2026-01-01"),
        )
        from app.dossier import repo as drepo
        self.drepo = drepo
        # 建一个完整卷宗 + 一个 partial 卷宗
        self.did_ready = drepo.create_dossier("trace-c", "u1", compile_rule_version="v1")
        drepo.update_dossier(self.did_ready, status="compiling")
        drepo.update_dossier(self.did_ready, status="ready",
                             manifest={"k": "v"}, facts={"topology": {}}, finished=True)
        self.did_partial = drepo.create_dossier("trace-c", "u1", compile_rule_version="v1")
        drepo.update_dossier(self.did_partial, status="compiling")
        drepo.update_dossier(self.did_partial, status="partial",
                             failure_reason="缺口", finished=True)

    def tearDown(self):
        db.execute("DELETE FROM evidence_dossiers WHERE trace_id='trace-c'")
        db.execute("DELETE FROM evaluation_sessions WHERE trace_id='trace-c'")
        db.execute("DELETE FROM runs WHERE trace_id='trace-c'")

    def test_start_rejects_nonexistent_dossier(self):
        from fastapi import HTTPException
        from app.eval_agent.api import EvalStartRequest, eval_start
        import asyncio
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(eval_start(EvalStartRequest(dossier_id="nonexist")))
        self.assertEqual(cm.exception.status_code, 404)

    def test_start_rejects_partial_dossier(self):
        from fastapi import HTTPException
        from app.eval_agent.api import EvalStartRequest, eval_start
        import asyncio
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(eval_start(EvalStartRequest(dossier_id=self.did_partial)))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("partial", cm.exception.detail)


class EvalDossierSealTest(unittest.TestCase):
    """需求 §30/R9：评估卷宗封存 + 完整性校验 + 分裂态防护。"""

    def setUp(self):
        db.execute(
            "INSERT OR IGNORE INTO runs(trace_id, workspace_id, status, owner_user_id, "
            "run_purpose, ingested_at) VALUES(?,?,?,?,?,?)",
            ("trace-seal", "ws", "completed", "u1", "user_generation", "2026-01-01"),
        )
        from app.dossier import repo as drepo
        self.did = drepo.create_dossier("trace-seal", "u1", compile_rule_version="v1")
        drepo.update_dossier(self.did, status="compiling")
        drepo.update_dossier(self.did, status="ready",
                             manifest={"k": "v"}, facts={"topology": {}}, finished=True)

    def tearDown(self):
        db.execute("DELETE FROM evaluation_dossiers WHERE trace_id='trace-seal'")
        db.execute("DELETE FROM evaluation_sessions WHERE trace_id='trace-seal'")
        db.execute("DELETE FROM evidence_dossiers WHERE trace_id='trace-seal'")
        db.execute("DELETE FROM runs WHERE trace_id='trace-seal'")

    def test_seal_fails_when_finding_missing_evidence_ref(self):
        """需求 §30：finding 缺 evidence_ref 则封存失败。"""
        from app.eval_agent.sealer import seal_evaluation_dossier, SealError
        with self.assertRaises(SealError):
            seal_evaluation_dossier(
                "eval-x1", self.did, 1, "trace-seal", "u1",
                findings=[{"id": "f01", "finding": "问题", "evidence_ref": []}],
            )

    def test_seal_success_writes_dossier_and_backfills_attempt(self):
        """封存成功：写 evaluation_dossiers + 回填尝试 completed + sealed_dossier_id。"""
        from app.eval_agent.sealer import seal_evaluation_dossier
        # 先建 session 行
        db.execute(
            "INSERT INTO evaluation_sessions(eval_id, trace_id, status, bound_dossier_id, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("eval-x2", "trace-seal", "running", self.did, "2026-01-01", "2026-01-01"),
        )
        evd = seal_evaluation_dossier(
            "eval-x2", self.did, 1, "trace-seal", "u1",
            findings=[{"id": "f01", "finding": "问题", "evidence_ref": ["evt-e1"]}],
            scores={"overall": 0.8}, report_md="# 报告",
        )
        # evaluation_dossiers 有行
        row = db.query_one(
            "SELECT seal_status, completeness_status, source_dossier_id FROM evaluation_dossiers "
            "WHERE dossier_id=?", (evd,)
        )
        self.assertEqual(row["seal_status"], "sealed")
        self.assertEqual(row["source_dossier_id"], self.did)
        # 尝试回填
        sess = db.query_one(
            "SELECT status, sealed_dossier_id FROM evaluation_sessions WHERE eval_id='eval-x2'"
        )
        self.assertEqual(sess["status"], "completed")
        self.assertEqual(sess["sealed_dossier_id"], evd)

    def test_one_attempt_one_dossier_unique(self):
        """需求 §37：一个评估尝试最多一份评估卷宗（UNIQUE 约束）。"""
        from app.eval_agent.sealer import seal_evaluation_dossier, SealError
        db.execute(
            "INSERT INTO evaluation_sessions(eval_id, trace_id, status, bound_dossier_id, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("eval-x3", "trace-seal", "running", self.did, "2026-01-01", "2026-01-01"),
        )
        seal_evaluation_dossier(
            "eval-x3", self.did, 1, "trace-seal", "u1",
            findings=[{"id": "f01", "finding": "x", "evidence_ref": ["evt-e1"]}],
        )
        # 同 attempt 二次封存应失败
        with self.assertRaises(SealError):
            seal_evaluation_dossier(
                "eval-x3", self.did, 1, "trace-seal", "u1",
                findings=[{"id": "f01", "finding": "x", "evidence_ref": ["evt-e1"]}],
            )


if __name__ == "__main__":
    unittest.main()
