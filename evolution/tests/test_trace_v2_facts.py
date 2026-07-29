from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import app.core.db as db
from app.core.settings import settings
from app.trace.facts import (
    ConsumptionRejected,
    add_lineage,
    append_outcome,
    append_release_event,
    append_score,
    lineage_for,
    require_ready_evidence_dossier,
    require_verified_creation_trace,
)


class TraceV2FactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = settings.evolution_db
        self.old_payload_dir = settings.trace_payload_dir
        settings.evolution_db = str(Path(self.tmp.name) / "evolution.db")
        settings.trace_payload_dir = str(Path(self.tmp.name) / "payloads")
        db._conn = None
        db.init_db()
        db.execute(
            """INSERT INTO runs
               (trace_id, workspace_id, thread_id, session_name, endpoint, status,
                started_at, event_count, ingested_at, schema_version, service, workload,
                integrity_status)
               VALUES ('trace-source', 'ws', 'thread', 'session', 'create', 'completed',
                       '2026-01-01T00:00:00+00:00', 1, '2026-01-01T00:00:01+00:00',
                       2, 'executor', 'creation', 'incomplete')"""
        )

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        settings.evolution_db = self.old_db
        settings.trace_payload_dir = self.old_payload_dir
        self.tmp.cleanup()

    def test_rejected_consumption_is_audited_before_workflow_start(self) -> None:
        with self.assertRaises(ConsumptionRejected) as caught:
            require_verified_creation_trace("trace-source")

        self.assertIn("integrity_status=verified", caught.exception.missing_fields)
        row = db.query_one("SELECT * FROM consumption_rejections")
        self.assertEqual(row["consumer_workload"], "evidence_compile")
        self.assertIn("integrity_status=verified", json.loads(row["missing_fields_json"]))

    def test_lineage_is_bidirectional_and_quality_facts_are_append_only(self) -> None:
        add_lineage("trace", "trace-source", "produces", "artifact_revision", "rev-1")
        graph = lineage_for("artifact_revision", "rev-1")
        self.assertEqual(graph["incoming"][0]["from_id"], "trace-source")

        outcome_id = append_outcome(
            target_type="artifact_revision",
            target_id="rev-1",
            outcome_type="adopt",
            actor_user_id="user-1",
            payload={"selected": True},
            outcome_id="outcome-1",
        )
        self.assertEqual(outcome_id, "outcome-1")
        self.assertEqual(
            append_outcome(
                target_type="artifact_revision",
                target_id="rev-1",
                outcome_type="adopt",
                actor_user_id="user-1",
                payload={"selected": True},
                outcome_id="outcome-1",
            ),
            "outcome-1",
        )
        first_score = append_score(
            target_type="artifact_revision",
            target_id="rev-1",
            rubric_id="human",
            rubric_version="1",
            score={"overall": 4},
            actor_user_id="user-1",
        )
        second_score = append_score(
            target_type="artifact_revision",
            target_id="rev-1",
            rubric_id="human",
            rubric_version="1",
            score={"overall": 5},
            actor_user_id="user-1",
            supersedes_score_id=first_score,
        )
        self.assertNotEqual(first_score, second_score)
        self.assertEqual(db.query_one("SELECT COUNT(*) AS count FROM score_records")["count"], 2)

    def test_release_state_records_commit_separately_from_activation(self) -> None:
        append_release_event(
            release_id="release-1", status="committed", candidate_id="candidate-1",
            actor_user_id="user-1",
        )
        with self.assertRaises(ValueError):
            append_release_event(
                release_id="release-1", status="activated", candidate_id="candidate-1",
                actor_user_id="user-1",
            )
        append_release_event(
            release_id="release-1", status="registry_promoted", candidate_id="candidate-1",
            actor_user_id="user-1",
        )
        append_release_event(
            release_id="release-1", status="executor_refresh_ack", candidate_id="candidate-1",
            actor_user_id="user-1",
        )
        append_release_event(
            release_id="release-1", status="activated", candidate_id="candidate-1",
            actor_user_id="user-1",
        )
        rows = db.query_all("SELECT status FROM release_events_v2 ORDER BY created_at, rowid")
        self.assertEqual(
            [row["status"] for row in rows],
            ["committed", "registry_promoted", "executor_refresh_ack", "activated"],
        )


class StrictReadyGateTest(unittest.TestCase):
    """AC-013 / CON-007 / DEC-007：只有完整 ready 卷宗可进入任何下游。

    覆盖七类资格：ready（接受）/ partial / incomplete / legacy / 编纂中 /
    已失效 / 资格查询失败（全部拒绝且 fail-closed）。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = settings.evolution_db
        self.old_payload_dir = settings.trace_payload_dir
        settings.evolution_db = str(Path(self.tmp.name) / "evolution.db")
        settings.trace_payload_dir = str(Path(self.tmp.name) / "payloads")
        db._conn = None
        db.init_db()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        settings.evolution_db = self.old_db
        settings.trace_payload_dir = self.old_payload_dir
        self.tmp.cleanup()

    def _insert_creation_trace(self, trace_id: str, integrity: str = "verified") -> None:
        db.execute(
            """INSERT INTO runs
               (trace_id, workspace_id, thread_id, session_name, endpoint, status,
                started_at, event_count, ingested_at, schema_version, service,
                workload, integrity_status)
               VALUES (?, 'ws', 'thread', 'session', 'create', 'completed',
                       '2026-01-01T00:00:00+00:00', 1, '2026-01-01T00:00:01+00:00',
                       2, 'executor', 'creation', ?)""",
            (trace_id, integrity),
        )

    def _insert_dossier(
        self, pack_id: str, trace_id: str, status: str,
        *, with_compile_trace: bool = True,
    ) -> None:
        compile_trace_id = f"{trace_id}-compile" if with_compile_trace else None
        if with_compile_trace:
            db.execute(
                """INSERT INTO runs
                   (trace_id, workspace_id, thread_id, session_name, endpoint, status,
                    started_at, event_count, ingested_at, schema_version, service,
                    workload, integrity_status)
                   VALUES (?, 'ws', 'thread', 'session', 'compile', 'completed',
                           '2026-01-01T00:00:02+00:00', 1, '2026-01-01T00:00:03+00:00',
                           2, 'evolution', 'evidence_compile', 'verified')""",
                (compile_trace_id,),
            )
        db.execute(
            """INSERT INTO evidence_dossiers
               (pack_id, trace_id, owner_user_id, version, is_current, status,
                provenance, compile_rule_version, manifest_json, facts_json, index_json,
                compile_trace_id, created_at)
               VALUES (?, ?, 'user-1', 1, 1, ?, 'trace_time', 'v1',
                       '{"ok":true}', '{"ok":true}', '{"ok":true}', ?, '2026-01-01T00:00:04+00:00')""",
            (pack_id, trace_id, status, compile_trace_id),
        )

    def test_ready_dossier_is_accepted(self) -> None:
        """ready 卷宗通过门禁（AC-013 接受侧）。"""
        self._insert_creation_trace("trace-ready")
        self._insert_dossier("pack-ready", "trace-ready", "ready")
        row = require_ready_evidence_dossier("pack-ready")
        self.assertIsNotNone(row)

    def test_partial_dossier_is_rejected(self) -> None:
        """AC-013 / DEC-007：partial 卷宗必须被拒绝，不得降级消费。"""
        self._insert_creation_trace("trace-partial")
        self._insert_dossier("pack-partial", "trace-partial", "partial")
        with self.assertRaises(ConsumptionRejected) as caught:
            require_ready_evidence_dossier("pack-partial")
        self.assertIn("status=ready", caught.exception.missing_fields)

    def test_incomplete_dossier_is_rejected(self) -> None:
        self._insert_creation_trace("trace-incomplete", integrity="incomplete")
        self._insert_dossier("pack-incomplete", "trace-incomplete", "ready")
        with self.assertRaises(ConsumptionRejected):
            require_ready_evidence_dossier("pack-incomplete")

    def test_compiling_dossier_is_rejected(self) -> None:
        """编纂中的卷宗不可消费。"""
        self._insert_creation_trace("trace-compiling")
        self._insert_dossier("pack-compiling", "trace-compiling", "compiling")
        with self.assertRaises(ConsumptionRejected):
            require_ready_evidence_dossier("pack-compiling")

    def test_failed_dossier_is_rejected(self) -> None:
        """编译失败的卷宗不可消费。"""
        self._insert_creation_trace("trace-failed")
        self._insert_dossier("pack-failed", "trace-failed", "failed")
        with self.assertRaises(ConsumptionRejected):
            require_ready_evidence_dossier("pack-failed")

    def test_nonexistent_dossier_fails_closed(self) -> None:
        """资格查询失败必须 fail-closed（EDGE-008）。"""
        with self.assertRaises(ConsumptionRejected):
            require_ready_evidence_dossier("pack-nonexistent")

    def test_rejection_is_audited(self) -> None:
        """拒绝必须写入 consumption_rejections 审计表（AC-013 可审计）。"""
        self._insert_creation_trace("trace-audit")
        self._insert_dossier("pack-audit", "trace-audit", "partial")
        with self.assertRaises(ConsumptionRejected):
            require_ready_evidence_dossier("pack-audit")
        row = db.query_one(
            "SELECT * FROM consumption_rejections WHERE source_id=?",
            ("pack-audit",),
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["consumer_workload"], "evaluation")


if __name__ == "__main__":
    unittest.main()
