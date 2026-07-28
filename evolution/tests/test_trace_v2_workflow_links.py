"""Trace V2 的异步工作流边界与血缘事实测试。"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["EVOLUTION_DB"] = _tmp_db.name
os.environ["EXECUTOR_URL"] = "http://127.0.0.1:0"

import app.core.db as db  # noqa: E402


def setUpModule() -> None:
    db._conn = None
    db.init_db()


def tearDownModule() -> None:
    db._conn = None
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


class _Recorder:
    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.create_kwargs = None
        self.completed = []
        self.failed = []

    def create_run(self, **kwargs):
        self.create_kwargs = kwargs
        return SimpleNamespace(trace_id=self.trace_id)

    def complete_run(self, trace_id):
        self.completed.append(trace_id)

    def fail_run(self, trace_id, error):
        self.failed.append((trace_id, str(error)))

    def append_business_event(self, *_args, **_kwargs):
        return None


class _Agent:
    async def ainvoke(self, *_args, **_kwargs):
        return {"messages": []}


class EvaluationTraceBoundaryTest(unittest.TestCase):
    def setUp(self):
        db.execute(
            "INSERT INTO evaluation_sessions"
            "(eval_id, trace_id, status, bound_dossier_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            ("eval-v2", "trace-source", "running", "evidence-v2", "now", "now"),
        )

    def tearDown(self):
        db.execute("DELETE FROM lineage_edges")
        db.execute("DELETE FROM evaluation_sessions")

    def test_evaluation_creates_linked_trace_and_consumes_dossier(self):
        from app.eval_agent.agent import run_eval_session
        from app.eval_agent.ctx import EvaluationContext

        recorder = _Recorder("trace-evaluation")
        ctx = EvaluationContext("eval-v2", "trace-source")
        ctx.recorder = recorder
        ctx.dossier_id = "evidence-v2"
        ctx.dossier_version = 3
        ctx.dossier = {"compile_trace_id": "trace-compile"}

        with patch("app.eval_agent.agent.build_eval_agent", return_value=_Agent()), patch(
            "app.eval_agent.agent.eval_repo.get_session", return_value={"status": "done"}
        ):
            result = asyncio.run(run_eval_session(ctx))

        self.assertEqual(result["status"], "done")
        self.assertEqual(recorder.create_kwargs["workload"], "evaluation")
        self.assertEqual(recorder.create_kwargs["external_refs"]["evaluation_id"], "eval-v2")
        link = recorder.create_kwargs["links"][0]
        self.assertEqual(link.target_trace_id, "trace-compile")
        self.assertEqual(link.relation, "consumes")
        edge = db.query_one(
            "SELECT relation FROM lineage_edges WHERE from_id=? AND to_id=?",
            ("trace-evaluation", "evidence-v2"),
        )
        self.assertEqual(edge["relation"], "consumes")


class EvaluationSealFactsTest(unittest.TestCase):
    def setUp(self):
        db.execute(
            "INSERT INTO evaluation_sessions"
            "(eval_id, trace_id, status, bound_dossier_id, self_trace_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("eval-seal-v2", "trace-source", "running", "evidence-v2",
             "trace-evaluation", "now", "now"),
        )

    def tearDown(self):
        db.execute("DELETE FROM lineage_edges")
        db.execute("DELETE FROM evaluation_dossiers")
        db.execute("DELETE FROM evaluation_sessions")

    def test_seal_writes_score_and_lineage_in_the_same_commit(self):
        from app.eval_agent.sealer import seal_evaluation_dossier

        dossier_id = seal_evaluation_dossier(
            "eval-seal-v2", "evidence-v2", 1, "trace-source", "u1",
            conclusions=[{"dimension": "quality", "result": "pass"}],
            scores={"overall": 0.9},
        )

        score = db.query_one("SELECT * FROM score_records WHERE target_id='trace-source'")
        self.assertIsNotNone(score)
        edges = db.query_all(
            "SELECT from_type, from_id, relation, to_type, to_id FROM lineage_edges ORDER BY id"
        )
        self.assertIn(
            {"from_type": "evidence_dossier", "from_id": "evidence-v2",
             "relation": "evaluated_by", "to_type": "evaluation_dossier", "to_id": dossier_id},
            edges,
        )
        self.assertIn(
            {"from_type": "trace", "from_id": "trace-evaluation", "relation": "produces",
             "to_type": "evaluation_dossier", "to_id": dossier_id},
            edges,
        )


class EvolutionTraceBoundaryTest(unittest.TestCase):
    def tearDown(self):
        db.execute("DELETE FROM lineage_edges")
        db.execute("DELETE FROM evolve_sessions")

    def test_evolution_creates_linked_trace_and_consumes_evaluation_dossier(self):
        from app.evolve import db as ev_db
        from app.evolve.agent.agent import run_evolve_session
        from app.evolve.ctx import EvolveContext

        ev_db.create_session("evolve-v2")
        recorder = _Recorder("trace-evolution")
        ctx = EvolveContext("evolve-v2")
        ctx.recorder = recorder
        ctx.eval_dossier_id = "evaluation-v2"
        ctx.eval_dossier = {
            "dossier_id": "evaluation-v2",
            "eval_attempt_id": "eval-v2",
            "evaluation_trace_id": "trace-evaluation",
        }
        ctx.design_doc_path = "design.md"
        ctx.change_log_path = "changes.md"

        async def _build(_ctx):
            return _Agent()

        with patch("app.evolve.agent.agent.build_evolve_agent", side_effect=_build):
            result = asyncio.run(run_evolve_session(ctx, "trace-source"))

        self.assertEqual(result["status"], "done")
        self.assertEqual(recorder.create_kwargs["workload"], "evolution")
        link = recorder.create_kwargs["links"][0]
        self.assertEqual(link.target_trace_id, "trace-evaluation")
        self.assertEqual(link.relation, "consumes")
        edge = db.query_one(
            "SELECT relation FROM lineage_edges WHERE from_id=? AND to_id=?",
            ("trace-evolution", "evaluation-v2"),
        )
        self.assertEqual(edge["relation"], "consumes")


if __name__ == "__main__":
    unittest.main()
