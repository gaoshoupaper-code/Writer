"""单阶段发版流程测试。

发版 = commit_candidate → probe_candidate（唯一门禁）→ promote_candidate →
commit_registry_and_push → reload_executor → session=published。

旧的 validate_candidate_snapshot（snapshot trace 三件套门禁）已移除——历史上
从未通过过，导致 session 永远卡 pending_review。本测试覆盖单阶段路径、probe 门禁
拒绝、激活失败回滚、幂等重入。
"""
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
    """单阶段发版：probe 通过即晋升 production + executor 热加载。"""

    def setUp(self) -> None:
        db.execute("DELETE FROM evolve_sessions")

    def _session(self, session_id: str) -> None:
        from app.evolve import db as ev_db
        ev_db.create_session(session_id)
        ev_db.update_session(session_id, status="pending_review")

    @staticmethod
    def _request():
        return SimpleNamespace(state=SimpleNamespace(user_id="developer-1"))

    def test_publish_promotes_in_one_stage(self) -> None:
        """单阶段：首次 publish 直接冻结 + probe + 晋升 + reload + published。"""
        from app.evolve.api import publish_session

        self._session("one-stage")
        probe_identity = {"identity_digest": "probe-digest"}
        gate_runtime = {"identity_digest": "probe-digest"}
        with patch("app.versioning.registry_repo.get_version_by_session", return_value=None), patch(
            "app.versioning.registry_repo.next_version_number", return_value=7
        ), patch("app.core.git_ops.commit_candidate", return_value="candidate-commit"), patch(
            "app.versioning.release_gate.probe_candidate",
            return_value={
                "harness_commit": "candidate-commit", "assembled": True,
                "artifact_snapshot_middleware": True,
                "runtime_identity": probe_identity,
            },
        ), patch(
            "app.versioning.registry_repo.create_candidate",
            return_value={"version": 7, "commit_hash": "candidate-commit", "source_session": "one-stage"},
        ), patch(
            "app.core.git_ops.commit_registry_and_push", return_value="registry-commit"
        ) as commit_registry, patch(
            "app.versioning.registry_repo.promote_candidate", return_value={"version": 7}
        ) as promote, patch(
            "app.versioning.snapshot_publisher.reload_executor",
            return_value={"commit": "candidate-commit", "runtime_identity": gate_runtime},
        ):
            result = publish_session("one-stage", self._request())

        self.assertEqual(result["status"], "activated")
        self.assertEqual(result["snapshot_version"], 7)
        promote.assert_called_once_with(7, snapshot_trace_id=None, runtime_identity=probe_identity)
        # commit_registry 调 2 次：注册 candidate + 晋升 production
        self.assertEqual(commit_registry.call_count, 2)
        session = db.query_one("SELECT status FROM evolve_sessions WHERE session_id='one-stage'")
        self.assertEqual(session["status"], "published")

    def test_probe_failure_rejects_publish(self) -> None:
        """probe 门禁失败（ValueError）→ 409，不晋升。"""
        from fastapi import HTTPException
        from app.evolve.api import publish_session

        self._session("probe-fail")
        with patch("app.versioning.registry_repo.get_version_by_session", return_value=None), patch(
            "app.versioning.registry_repo.next_version_number", return_value=8
        ), patch("app.core.git_ops.commit_candidate", return_value="candidate-commit"), patch(
            "app.versioning.release_gate.probe_candidate",
            side_effect=ValueError("candidate clean-checkout probe failed: assembled=False"),
        ), patch("app.versioning.registry_repo.promote_candidate") as promote:
            with self.assertRaises(HTTPException) as caught:
                publish_session("probe-fail", self._request())

        self.assertEqual(caught.exception.status_code, 409)
        promote.assert_not_called()
        session = db.query_one("SELECT status FROM evolve_sessions WHERE session_id='probe-fail'")
        self.assertEqual(session["status"], "pending_review")

    def test_activation_failure_restores_previous_production_and_stays_retryable(self) -> None:
        """reload_executor 激活失败（commit mismatch）→ 回滚原 production + session 退回 pending_review。"""
        from fastapi import HTTPException
        from app.evolve.api import publish_session

        self._session("activation-restore")
        probe_identity = {"identity_digest": "candidate-runtime"}
        with patch("app.versioning.registry_repo.get_version_by_session", return_value=None), patch(
            "app.versioning.registry_repo.next_version_number", return_value=10
        ), patch("app.core.git_ops.commit_candidate", return_value="candidate-commit"), patch(
            "app.versioning.release_gate.probe_candidate",
            return_value={
                "harness_commit": "candidate-commit", "assembled": True,
                "runtime_identity": probe_identity,
            },
        ), patch(
            "app.versioning.registry_repo.create_candidate",
            return_value={"version": 10, "commit_hash": "candidate-commit", "source_session": "activation-restore"},
        ), patch(
            "app.versioning.registry_repo.get_production_version_number", return_value=6
        ), patch(
            "app.versioning.registry_repo.promote_candidate", return_value={"version": 10}
        ), patch(
            "app.versioning.registry_repo.restore_production"
        ) as restore, patch(
            "app.core.git_ops.commit_registry_and_push", return_value="registry-commit"
        ) as commit_registry, patch(
            "app.versioning.snapshot_publisher.reload_executor",
            side_effect=[
                {"commit": "wrong-commit", "runtime_identity": probe_identity},
                {"commit": "production-commit", "runtime_identity": {}},
            ],
        ), patch(
            "app.versioning.registry_repo.get_production_version",
            return_value={"version": 6, "commit_hash": "production-commit"},
        ):
            with self.assertRaises(HTTPException) as caught:
                publish_session("activation-restore", self._request())

        self.assertEqual(caught.exception.status_code, 502)
        restore.assert_called_once_with(6, 10)
        self.assertEqual(commit_registry.call_count, 3)
        session = db.query_one("SELECT status FROM evolve_sessions WHERE session_id='activation-restore'")
        self.assertEqual(session["status"], "pending_review")

    def test_manual_rollback_verifies_exact_executor_identity(self) -> None:
        """rollback（snapshot_api）路径不受单阶段化影响，仍校验 identity。"""
        from app.versioning.snapshot_api import RollbackRequest, rollback_snapshot

        current = {"version": 8, "commit_hash": "current-commit"}
        target = {
            "version": 6, "commit_hash": "target-commit",
            "runtime_identity": {"identity_digest": "target-runtime"},
        }
        with patch("app.versioning.registry_repo.get_production_version", return_value=current), \
             patch("app.versioning.registry_repo.rollback", return_value=target), \
             patch("app.core.git_ops.commit_registry_and_push", return_value="registry-commit"), \
             patch("app.versioning.snapshot_publisher.reload_executor",
                   return_value={"commit": "target-commit", "runtime_identity": target["runtime_identity"]}):
            result = rollback_snapshot(
                RollbackRequest(to_version=6, reason="regression"), self._request()
            )

        self.assertEqual(result["status"], "rollback_activated")
        self.assertEqual(result["source_commit"], "target-commit")
        self.assertEqual(result["registry_commit"], "registry-commit")

    def test_already_published_is_idempotent(self) -> None:
        """已发布（candidate 已 production）再次 publish → 幂等返回 activated，不重复晋升。"""
        from app.evolve.api import publish_session

        self._session("idempotent")
        candidate = {
            "version": 11, "commit_hash": "published-commit", "source_session": "idempotent",
            "status": "production",
        }
        with patch("app.versioning.registry_repo.get_version_by_session", return_value=candidate), \
             patch("app.versioning.registry_repo.promote_candidate") as promote, \
             patch("app.versioning.snapshot_publisher.reload_executor") as reload:
            result = publish_session("idempotent", self._request())

        self.assertEqual(result["status"], "activated")
        self.assertEqual(result["snapshot_version"], 11)
        promote.assert_not_called()
        reload.assert_not_called()
        session = db.query_one("SELECT status FROM evolve_sessions WHERE session_id='idempotent'")
        self.assertEqual(session["status"], "published")


if __name__ == "__main__":
    unittest.main()
