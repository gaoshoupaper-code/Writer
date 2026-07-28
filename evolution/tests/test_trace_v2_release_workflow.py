from __future__ import annotations

import os
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
    def _session(self, session_id: str) -> None:
        from app.evolve import db as ev_db
        ev_db.create_session(session_id)
        ev_db.update_session(session_id, status="pending_review")

    def _request(self):
        return SimpleNamespace(state=SimpleNamespace(user_id="developer-1"))

    def test_publish_records_activation_only_after_executor_ack(self) -> None:
        from app.evolve.api import publish_session

        self._session("release-ok")
        with patch("app.versioning.registry_repo.publish_version", return_value={"version": 7}), patch(
            "app.core.git_ops.commit_and_push", return_value="abc123"
        ), patch("app.versioning.snapshot_publisher.notify_executor", return_value=True):
            result = publish_session("release-ok", self._request())

        self.assertEqual(result["status"], "activated")
        rows = db.query_all(
            "SELECT status FROM release_events_v2 WHERE release_id=? ORDER BY rowid",
            ("release-release-ok",),
        )
        self.assertEqual(
            [row["status"] for row in rows],
            ["committed", "registry_promoted", "executor_refresh_ack", "activated"],
        )

    def test_publish_records_activation_failure_without_marking_published(self) -> None:
        from fastapi import HTTPException
        from app.evolve.api import publish_session

        self._session("release-fail")
        with patch("app.versioning.registry_repo.publish_version", return_value={"version": 8}), patch(
            "app.core.git_ops.commit_and_push", return_value="def456"
        ), patch("app.versioning.snapshot_publisher.notify_executor", return_value=False):
            with self.assertRaises(HTTPException) as caught:
                publish_session("release-fail", self._request())

        self.assertEqual(caught.exception.status_code, 502)
        rows = db.query_all(
            "SELECT status FROM release_events_v2 WHERE release_id=? ORDER BY rowid",
            ("release-release-fail",),
        )
        self.assertEqual(
            [row["status"] for row in rows],
            ["committed", "registry_promoted", "activation_failed"],
        )
        session = db.query_one(
            "SELECT status FROM evolve_sessions WHERE session_id='release-fail'"
        )
        self.assertEqual(session["status"], "failed")

    def test_rollback_is_recorded_only_after_executor_ack(self) -> None:
        from app.trace.facts import append_release_event
        from app.versioning.snapshot_api import RollbackRequest, rollback_snapshot

        for status in ("committed", "registry_promoted", "executor_refresh_ack", "activated"):
            append_release_event(
                release_id="release-source",
                status=status,
                candidate_id="harness-version-8",
                actor_user_id="developer-1",
            )
        with patch(
            "app.versioning.registry_repo.get_production_version",
            return_value={"version": 8},
        ), patch(
            "app.versioning.registry_repo.rollback", return_value={"version": 7}
        ), patch("app.core.git_ops.commit_and_push", return_value="rollback789"), patch(
            "app.versioning.snapshot_publisher.notify_executor", return_value=True
        ):
            result = rollback_snapshot(
                RollbackRequest(to_version=7, reason="regression"), self._request()
            )

        self.assertEqual(result["status"], "rollback_activated")
        row = db.query_one(
            "SELECT status FROM release_events_v2 WHERE release_id=? ORDER BY rowid DESC LIMIT 1",
            ("release-source",),
        )
        self.assertEqual(row["status"], "rollback_activated")


if __name__ == "__main__":
    unittest.main()
