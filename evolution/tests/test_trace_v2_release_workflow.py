from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys_path = str(Path(__file__).resolve().parent.parent)
import sys
sys.path.insert(0, sys_path)

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


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        db.execute("DELETE FROM evolve_sessions")

    def _session(self, session_id: str) -> None:
        from app.evolve import db as ev_db

        ev_db.create_session(session_id)
        ev_db.update_session(session_id, status="pending_review")

    @staticmethod
    def _request():
        return SimpleNamespace(state=SimpleNamespace(user_id="developer-1"))

    def test_first_publish_freezes_candidate_without_moving_production(self) -> None:
        from app.evolve.api import publish_session

        self._session("candidate-only")
        candidate = {
            "version": 7,
            "commit_hash": "candidate-commit",
            "source_session": "candidate-only",
        }
        with patch("app.versioning.registry_repo.get_version_by_session", return_value=None), patch(
            "app.versioning.registry_repo.next_version_number", return_value=7
        ), patch("app.core.git_ops.commit_candidate", return_value="candidate-commit"), patch(
            "app.versioning.registry_repo.create_candidate", return_value=candidate
        ) as create_candidate, patch(
            "app.core.git_ops.commit_registry_and_push", return_value="registry-commit"
        ), patch(
            "app.versioning.release_gate.probe_candidate",
            return_value={"harness_commit": "candidate-commit", "assembled": True},
        ), patch("app.versioning.registry_repo.promote_candidate") as promote:
            result = publish_session("candidate-only", self._request())

        self.assertEqual(result["status"], "candidate_pending_snapshot")
        self.assertEqual(result["snapshot_version"], 7)
        create_candidate.assert_called_once()
        promote.assert_not_called()
        session = db.query_one(
            "SELECT status FROM evolve_sessions WHERE session_id='candidate-only'"
        )
        self.assertEqual(session["status"], "pending_review")

    def test_second_publish_promotes_only_the_validated_candidate(self) -> None:
        from app.evolve.api import publish_session

        self._session("promote-ok")
        candidate = {
            "version": 8,
            "commit_hash": "candidate-commit",
            "source_session": "promote-ok",
        }
        gate = {
            "snapshot_trace_id": "trace-snapshot",
            "runtime_identity": {"identity_digest": "runtime-digest"},
        }
        with patch(
            "app.versioning.registry_repo.get_version_by_session", return_value=candidate
        ), patch(
            "app.versioning.release_gate.probe_candidate",
            return_value={"harness_commit": "candidate-commit", "assembled": True},
        ), patch(
            "app.versioning.release_gate.validate_candidate_snapshot", return_value=gate
        ), patch(
            "app.versioning.registry_repo.promote_candidate", return_value=candidate
        ) as promote, patch(
            "app.core.git_ops.commit_registry_and_push", return_value="registry-promotion"
        ), patch(
            "app.versioning.snapshot_publisher.reload_executor",
            return_value={"commit": "candidate-commit", "runtime_identity": gate["runtime_identity"]},
        ):
            result = publish_session("promote-ok", self._request())

        self.assertEqual(result["status"], "activated")
        promote.assert_called_once_with(
            8,
            snapshot_trace_id="trace-snapshot",
            runtime_identity=gate["runtime_identity"],
        )
        rows = db.query_all(
            "SELECT status FROM release_events_v2 WHERE release_id=? ORDER BY rowid",
            ("release-promote-ok",),
        )
        self.assertEqual(
            [row["status"] for row in rows],
            ["committed", "registry_promoted", "executor_refresh_ack", "activated"],
        )

    def test_identity_mismatch_cannot_move_production(self) -> None:
        from fastapi import HTTPException
        from app.evolve.api import publish_session

        self._session("promote-reject")
        candidate = {
            "version": 9,
            "commit_hash": "candidate-commit",
            "source_session": "promote-reject",
        }
        with patch(
            "app.versioning.registry_repo.get_version_by_session", return_value=candidate
        ), patch(
            "app.versioning.release_gate.probe_candidate",
            return_value={"harness_commit": "candidate-commit", "assembled": True},
        ), patch(
            "app.versioning.release_gate.validate_candidate_snapshot",
            side_effect=ValueError("dependency_lock_digest mismatch"),
        ), patch(
            "app.core.git_ops.commit_registry_and_push", return_value="registry-commit"
        ), patch("app.versioning.registry_repo.promote_candidate") as promote:
            with self.assertRaises(HTTPException) as caught:
                publish_session("promote-reject", self._request())

        self.assertEqual(caught.exception.status_code, 409)
        promote.assert_not_called()

    def test_activation_failure_restores_previous_production_and_stays_retryable(self) -> None:
        from fastapi import HTTPException
        from app.evolve.api import publish_session

        self._session("activation-restore")
        candidate = {
            "version": 10,
            "commit_hash": "candidate-commit",
            "source_session": "activation-restore",
        }
        gate = {
            "snapshot_trace_id": "trace-snapshot",
            "runtime_identity": {"identity_digest": "candidate-runtime"},
        }
        with patch(
            "app.versioning.registry_repo.get_version_by_session", return_value=candidate
        ), patch(
            "app.versioning.release_gate.probe_candidate",
            return_value={"harness_commit": "candidate-commit", "assembled": True},
        ), patch(
            "app.versioning.release_gate.validate_candidate_snapshot", return_value=gate
        ), patch(
            "app.versioning.registry_repo.get_production_version_number", return_value=6
        ), patch(
            "app.versioning.registry_repo.get_production_version",
            return_value={"version": 6, "commit_hash": "production-commit"},
        ), patch(
            "app.versioning.registry_repo.promote_candidate", return_value=candidate
        ), patch(
            "app.versioning.registry_repo.restore_production"
        ) as restore, patch(
            "app.core.git_ops.commit_registry_and_push", return_value="registry-commit"
        ) as commit_registry, patch(
            "app.versioning.snapshot_publisher.reload_executor",
            side_effect=[
                {"commit": "wrong-commit", "runtime_identity": gate["runtime_identity"]},
                {"commit": "production-commit", "runtime_identity": {}},
            ],
        ):
            with self.assertRaises(HTTPException) as caught:
                publish_session("activation-restore", self._request())

        self.assertEqual(caught.exception.status_code, 502)
        restore.assert_called_once_with(6, 10)
        self.assertEqual(commit_registry.call_count, 3)
        session = db.query_one(
            "SELECT status FROM evolve_sessions WHERE session_id='activation-restore'"
        )
        self.assertEqual(session["status"], "pending_review")

    def test_manual_rollback_verifies_exact_executor_identity(self) -> None:
        from app.versioning.snapshot_api import RollbackRequest, rollback_snapshot

        current = {"version": 8, "commit_hash": "current-commit"}
        target = {
            "version": 6,
            "commit_hash": "target-commit",
            "runtime_identity": {"identity_digest": "target-runtime"},
        }
        with patch(
            "app.versioning.registry_repo.get_production_version", return_value=current
        ), patch(
            "app.versioning.registry_repo.rollback", return_value=target
        ), patch(
            "app.core.git_ops.commit_registry_and_push", return_value="registry-commit"
        ), patch(
            "app.versioning.snapshot_publisher.reload_executor",
            return_value={
                "commit": "target-commit",
                "runtime_identity": target["runtime_identity"],
            },
        ):
            result = rollback_snapshot(
                RollbackRequest(to_version=6, reason="regression"), self._request()
            )

        self.assertEqual(result["status"], "rollback_activated")
        self.assertEqual(result["source_commit"], "target-commit")
        self.assertEqual(result["registry_commit"], "registry-commit")

    def test_retry_after_registry_promotion_reuses_the_same_version(self) -> None:
        from app.evolve.api import publish_session
        from app.trace.facts import append_release_event

        self._session("resume-promoted")
        candidate = {
            "version": 11,
            "commit_hash": "candidate-commit",
            "source_session": "resume-promoted",
            "status": "production",
            "parent_version": 6,
        }
        gate = {
            "snapshot_trace_id": "trace-snapshot",
            "runtime_identity": {"identity_digest": "runtime-digest"},
        }
        append_release_event(
            release_id="release-resume-promoted",
            status="committed",
            candidate_id="harness-version-11",
            actor_user_id="developer-1",
        )
        append_release_event(
            release_id="release-resume-promoted",
            status="registry_promoted",
            candidate_id="harness-version-11",
            actor_user_id="developer-1",
        )
        with patch(
            "app.versioning.registry_repo.get_version_by_session", return_value=candidate
        ), patch(
            "app.versioning.release_gate.probe_candidate",
            return_value={"harness_commit": "candidate-commit", "assembled": True},
        ), patch(
            "app.versioning.release_gate.validate_candidate_snapshot", return_value=gate
        ), patch(
            "app.versioning.registry_repo.promote_candidate"
        ) as promote, patch(
            "app.core.git_ops.commit_registry_and_push", return_value="registry-commit"
        ), patch(
            "app.versioning.snapshot_publisher.reload_executor",
            return_value={"commit": "candidate-commit", "runtime_identity": gate["runtime_identity"]},
        ):
            result = publish_session("resume-promoted", self._request())

        self.assertEqual(result["snapshot_version"], 11)
        promote.assert_not_called()
        rows = db.query_all(
            "SELECT status FROM release_events_v2 WHERE release_id=? ORDER BY rowid",
            ("release-resume-promoted",),
        )
        self.assertEqual(
            [row["status"] for row in rows],
            ["committed", "registry_promoted", "executor_refresh_ack", "activated"],
        )


class ReleaseGateValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        db.execute("DELETE FROM evaluation_dossiers")
        db.execute("DELETE FROM evidence_dossiers")
        db.execute("DELETE FROM manual_tests")
        db.execute("DELETE FROM runs")

    def _complete_snapshot(self) -> tuple[dict, dict]:
        runtime_identity = {
            "harness_commit": "candidate-commit",
            "harness_dirty": False,
            "artifact_snapshot_middleware": True,
            "platform_artifact_capture": True,
            "identity_digest": "runtime-digest",
        }
        db.execute(
            """INSERT INTO runs
               (trace_id, workspace_id, status, owner_user_id, run_purpose, ingested_at,
                integrity_status, evidence_status, run_snapshot_json)
               VALUES (?, ?, 'completed', ?, 'user_generation', ?, 'verified', 'complete', ?)""",
            (
                "trace-snapshot",
                "workspace-1",
                "developer-1",
                "2026-07-30T00:00:00+00:00",
                json.dumps(runtime_identity),
            ),
        )
        db.execute(
            """INSERT INTO manual_tests
               (test_id, case_id, version_type, version_id, trace_id, status, created_at)
               VALUES (?, ?, 'snapshot', 12, ?, 'done', ?)""",
            (
                "test-snapshot",
                "case-1",
                "trace-snapshot",
                "2026-07-30T00:00:00+00:00",
            ),
        )
        db.execute(
            """INSERT INTO evidence_dossiers
               (pack_id, trace_id, owner_user_id, version, status, provenance,
                compile_rule_version, created_at)
               VALUES (?, ?, ?, 1, 'ready', 'trace_time', 'v2', ?)""",
            (
                "evidence-ready",
                "trace-snapshot",
                "developer-1",
                "2026-07-30T00:00:00+00:00",
            ),
        )
        db.execute(
            """INSERT INTO evaluation_dossiers
               (dossier_id, eval_attempt_id, source_dossier_id, source_dossier_version,
                trace_id, owner_user_id, completeness_status, seal_status, created_at)
               VALUES (?, ?, ?, 1, ?, ?, 'complete', 'sealed', ?)""",
            (
                "evaluation-sealed",
                "eval-1",
                "evidence-ready",
                "trace-snapshot",
                "developer-1",
                "2026-07-30T00:00:00+00:00",
            ),
        )
        candidate = {
            "version": 12,
            "commit_hash": "candidate-commit",
            "probe_identity": runtime_identity,
        }
        probe = {"runtime_identity": runtime_identity}
        return candidate, probe

    def test_complete_snapshot_with_same_runtime_identity_passes(self) -> None:
        from app.versioning.release_gate import validate_candidate_snapshot

        candidate, probe = self._complete_snapshot()

        result = validate_candidate_snapshot(candidate, probe)

        self.assertEqual(result["snapshot_trace_id"], "trace-snapshot")
        self.assertEqual(result["evidence_dossier_id"], "evidence-ready")
        self.assertEqual(result["evaluation_dossier_id"], "evaluation-sealed")
        self.assertEqual(result["runtime_identity"]["identity_digest"], "runtime-digest")

    def test_candidate_probe_identity_drift_is_rejected(self) -> None:
        from app.versioning.release_gate import validate_candidate_snapshot

        candidate, probe = self._complete_snapshot()
        candidate["probe_identity"] = {"identity_digest": "frozen-runtime-digest"}

        with self.assertRaisesRegex(
            ValueError, "executor identity changed since candidate freeze"
        ):
            validate_candidate_snapshot(candidate, probe)


if __name__ == "__main__":
    unittest.main()
