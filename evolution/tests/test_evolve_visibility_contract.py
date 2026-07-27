"""进化可见性隔离契约测试（2026-07-27，证据分级可见性重构阶段 D）。

阶段 D：进化 Agent 只读评估卷宗，不读原始 trace / 完整证据卷宗。
固化切断后的不变量，防止回退：

  1. 进化工具集不含 read_trace（旁路切断）
  2. flow.py 不引用 get_trace / _read_memory_quality_summary
  3. 进化启动按 eval_dossier_id；不存在/未封存/无 findings 一律拒（§42）
  4. 进化会话永久绑定 bound_eval_dossier_id（§42，创建后不可变）
  5. 评估卷宗封存时冻结 finding 引用的证据片段（§22，供进化归因）

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


class EvolveToolsetVisibilityTest(unittest.TestCase):
    """需求 R1：进化 Agent 工具集不含 trace 直读旁路。"""

    def test_no_read_trace_in_flow_tools(self):
        from app.evolve.agent.tools.flow import make_flow_tools
        tools = make_flow_tools()
        names = [t.name for t in tools]
        self.assertNotIn("read_trace", names, "flow 工具集仍含 read_trace（旁路未切断）")
        # 应有：read_eval_report / read_evidence_pack / write_design_doc / validate_changes / write_change_log
        self.assertIn("read_eval_report", names)
        self.assertIn("read_evidence_pack", names)

    def test_flow_does_not_reference_trace_bypass(self):
        """flow.py 不应引用 get_trace / _read_memory_quality_summary（AST 检查实际引用）。"""
        import ast
        import inspect
        from app.evolve.agent.tools import flow
        tree = ast.parse(inspect.getsource(flow))
        referenced: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    referenced.add(alias.asname or alias.name)
        self.assertNotIn("get_trace", referenced, "flow.py 引用 get_trace（trace 旁路）")
        self.assertNotIn("_read_memory_quality_summary", referenced,
                         "flow.py 引用 _read_memory_quality_summary（直 SQL 旁路）")
        self.assertNotIn("load_trace_detail", referenced)


class EvolveStartDossierGateTest(unittest.TestCase):
    """需求 §42：进化按评估卷宗启动，永久绑定。"""

    def setUp(self):
        db.execute(
            "INSERT OR IGNORE INTO runs(trace_id, workspace_id, status, owner_user_id, "
            "run_purpose, ingested_at) VALUES(?,?,?,?,?,?)",
            ("trace-d", "ws", "completed", "u1", "user_generation", "2026-01-01"),
        )
        from app.dossier import repo as drepo
        self.did = drepo.create_dossier("trace-d", "u1", compile_rule_version="v1")
        drepo.update_dossier(self.did, status="compiling")
        drepo.update_dossier(self.did, status="ready",
                             manifest={}, facts={}, finished=True)
        # 评估尝试 + 封存评估卷宗
        db.execute(
            "INSERT INTO evaluation_sessions(eval_id, trace_id, status, bound_dossier_id, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("eval-d", "trace-d", "running", self.did, "2026-01-01", "2026-01-01"),
        )
        from app.eval_agent.sealer import seal_evaluation_dossier
        self.evd = seal_evaluation_dossier(
            "eval-d", self.did, 1, "trace-d", "u1",
            findings=[{"id": "f01", "finding": "问题", "evidence_ref": ["evt-e1"]}],
            frozen_evidence={"evt-e1": {"type": "tool_end", "agent_name": "writing-subagent"}},
        )

    def tearDown(self):
        db.execute("DELETE FROM evaluation_dossiers WHERE trace_id='trace-d'")
        db.execute("DELETE FROM evaluation_sessions WHERE trace_id='trace-d'")
        db.execute("DELETE FROM evolve_sessions WHERE baseline_trace='trace-d' OR session_id LIKE 'test-%'")
        db.execute("DELETE FROM evidence_dossiers WHERE trace_id='trace-d'")
        db.execute("DELETE FROM runs WHERE trace_id='trace-d'")

    def test_resolve_rejects_nonexistent_dossier(self):
        from fastapi import HTTPException
        from app.evolve.api import _resolve_eval_dossier
        with self.assertRaises(HTTPException) as cm:
            _resolve_eval_dossier("nonexist")
        self.assertEqual(cm.exception.status_code, 404)

    def test_resolve_returns_sealed_dossier_with_frozen_evidence(self):
        from app.evolve.api import _resolve_eval_dossier
        d = _resolve_eval_dossier(self.evd)
        self.assertEqual(d["seal_status"], "sealed")
        self.assertEqual(len(d["findings"]), 1)
        self.assertIn("evt-e1", d.get("frozen_evidence", {}))

    def test_evolve_start_binds_dossier_permanently(self):
        """进化启动永久绑定 bound_eval_dossier_id（§42）。"""
        import asyncio
        from app.evolve.api import EvolveStartRequest, evolve_start_converse
        resp = asyncio.run(evolve_start_converse(
            EvolveStartRequest(eval_dossier_id=self.evd)
        ))
        self.assertEqual(resp.eval_dossier_id, self.evd)
        sess = db.query_one(
            "SELECT bound_eval_dossier_id FROM evolve_sessions WHERE session_id=?",
            (resp.session_id,),
        )
        self.assertEqual(sess["bound_eval_dossier_id"], self.evd)


class EvalDossierFrozenEvidenceTest(unittest.TestCase):
    """需求 §22：评估卷宗冻结 finding 引用的证据片段（供进化归因）。"""

    def test_frozen_evidence_column_exists(self):
        import sqlite3
        conn = sqlite3.connect(_tmp_db.name)
        try:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(evaluation_dossiers)"
            ).fetchall()]
            self.assertIn("frozen_evidence_json", cols)
        finally:
            conn.close()

    def test_collect_frozen_evidence_only_allows_indexed_ids(self):
        """collect_frozen_evidence 只冻结证据卷宗 index 内的 ID（受控）。"""
        from app.eval_agent.sealer import collect_frozen_evidence
        # 准备一条 event_payloads
        import json
        db.execute(
            "INSERT OR IGNORE INTO runs(trace_id, workspace_id, status, owner_user_id, "
            "run_purpose, ingested_at) VALUES(?,?,?,?,?,?)",
            ("trace-fr", "ws", "completed", "u1", "user_generation", "2026-01-01"),
        )
        db.execute(
            "DELETE FROM event_payloads WHERE trace_id='trace-fr' AND sequence=1"
        )
        db.execute(
            "INSERT INTO event_payloads(trace_id, sequence, type, timestamp, payload_json) "
            "VALUES(?,?,?,?,?)",
            ("trace-fr", 1, "tool_end", "2026-01-01",
             json.dumps({"event_id": "e1", "type": "tool_end",
                         "agent_name": "writing-subagent",
                         "tool_output": {"content": "正文片段"}})),
        )
        try:
            findings = [{"id": "f01", "evidence_ref": ["evt-e1"]}]
            # evt-e1 在 allowed 内 → 冻结
            frozen = collect_frozen_evidence(findings, [], "trace-fr", {"evt-e1"})
            self.assertIn("evt-e1", frozen)
            self.assertEqual(frozen["evt-e1"]["agent_name"], "writing-subagent")
            # evt-e2 不在 allowed 内 → 不冻结
            frozen2 = collect_frozen_evidence(
                [{"id": "f02", "evidence_ref": ["evt-e2"]}], [], "trace-fr", {"evt-e1"},
            )
            self.assertNotIn("evt-e2", frozen2)
        finally:
            db.execute("DELETE FROM event_payloads WHERE trace_id='trace-fr'")
            db.execute("DELETE FROM runs WHERE trace_id='trace-fr'")


if __name__ == "__main__":
    unittest.main()
