from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import app.core.db as db
from app.core.settings import settings
from app.dossier.api import CompileStartRequest, list_candidates, start_compile
from app.dossier.eligibility import assess_creation_trace
from contracts.trace import TraceLogEvent
from contracts.trace.payload import ContentAddressedPayloadStore
from fastapi import HTTPException


class DossierTraceEligibilityTest(unittest.TestCase):
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

    def test_candidates_and_direct_start_share_one_source_boundary(self) -> None:
        self._seed_complete_creation("trace-creation")
        for trace_id, service, workload, purpose in (
            ("trace-compile", "evolution", "evidence_compile", "evidence_compile"),
            ("trace-eval", "evolution", "evaluation", "evolution_eval"),
            ("trace-evolve", "evolution", "evolution", "evolution_evolve"),
            ("trace-infra", "executor", None, "infrastructure"),
        ):
            self._seed_run(trace_id, service=service, workload=workload, purpose=purpose)

        response = list_candidates(limit=100, offset=0)

        self.assertEqual([item["trace_id"] for item in response["items"]], ["trace-creation"])
        for trace_id in ("trace-compile", "trace-eval", "trace-evolve", "trace-infra"):
            before = db.query_one("SELECT COUNT(*) AS count FROM evidence_dossiers")["count"]
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(start_compile(CompileStartRequest(trace_id=trace_id)))
            self.assertEqual(caught.exception.status_code, 409)
            after = db.query_one("SELECT COUNT(*) AS count FROM evidence_dossiers")["count"]
            self.assertEqual(after, before)

    def test_transport_verified_without_revisions_is_not_consumable(self) -> None:
        self._seed_run("trace-missing-revisions")
        self._seed_contract("trace-missing-revisions")
        self._seed_tool_end("trace-missing-revisions", "call-missing", "/chapter/chapter-01.md")

        report = assess_creation_trace("trace-missing-revisions")

        self.assertEqual(report.transport_integrity, "verified")
        self.assertEqual(report.evidence_status, "incomplete")
        self.assertIn("artifact_revision_missing:1", report.missing_fields)
        self.assertNotIn(
            "trace-missing-revisions",
            [item["trace_id"] for item in list_candidates(limit=100, offset=0)["items"]],
        )
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(start_compile(CompileStartRequest(trace_id="trace-missing-revisions")))
        self.assertIn("artifact_revision_missing:1", caught.exception.detail["missing_fields"])

    def test_tool_message_error_is_not_counted_as_successful_write(self) -> None:
        self._seed_run("trace-tool-error")
        self._seed_contract("trace-tool-error")
        self._seed_tool_end(
            "trace-tool-error",
            "call-error",
            "/chapter/chapter-01.md",
            tool_output={"status": "error", "error": "disk full"},
        )

        report = assess_creation_trace("trace-tool-error")

        self.assertEqual(report.successful_write_count, 0)
        self.assertIn("successful_artifact_write", report.missing_fields)
        self.assertNotIn("artifact_revision_missing:1", report.missing_fields)

    def test_complete_contract_write_revision_and_payload_are_consumable(self) -> None:
        self._seed_complete_creation("trace-complete")

        report = assess_creation_trace("trace-complete")

        self.assertTrue(report.eligible)
        self.assertEqual(report.evidence_status, "complete")
        self.assertEqual(report.successful_write_count, 1)
        self.assertEqual(report.artifact_revision_count, 1)

    def test_compile_lineage_includes_recovery_revision(self) -> None:
        from app.dossier.api import _record_compile_lineage

        self._seed_complete_creation("trace-recovered")
        db.execute(
            """UPDATE artifact_revisions
               SET producer_trace_id='trace-recovery', source_trace_id='trace-recovered',
                   provenance='trace_payload_recovery'
               WHERE artifact_revision_id='revision-trace-recovered'"""
        )

        _record_compile_lineage("trace-recovered", "dossier-recovered", "trace-compile")

        edge = db.query_one(
            """SELECT relation FROM lineage_edges
               WHERE from_type='artifact_revision' AND from_id='revision-trace-recovered'
                 AND to_type='evidence_dossier' AND to_id='dossier-recovered'"""
        )
        self.assertIsNotNone(edge)
        self.assertEqual(edge["relation"], "compiled_into")

    def _seed_complete_creation(self, trace_id: str) -> None:
        self._seed_run(trace_id)
        self._seed_contract(trace_id)
        self._seed_tool_end(trace_id, "call-1", "/chapter/chapter-01.md")
        self._seed_revision(trace_id, "call-1", "/chapter/chapter-01.md", "正文")

    def _seed_run(
        self,
        trace_id: str,
        *,
        service: str | None = "executor",
        workload: str | None = "creation",
        purpose: str = "user_generation",
    ) -> None:
        db.execute(
            """INSERT INTO runs
               (trace_id, workspace_id, thread_id, session_name, endpoint, status,
                started_at, ended_at, event_count, ingested_at, owner_user_id,
                run_purpose, schema_version, service, workload, integrity_status)
               VALUES (?, 'ws', 'thread', 'session', 'screenplay.generate.stream', 'completed',
                       ?, ?, 0, ?, 'owner', ?, 2, ?, ?, 'verified')""",
            (trace_id, self._now(), self._now(), self._now(), purpose, service, workload),
        )

    def _seed_contract(self, trace_id: str) -> None:
        self._seed_event(
            TraceLogEvent(
                trace_id=trace_id,
                event_id=f"{trace_id}-contract",
                sequence=1,
                type="run_meta",
                status="completed",
                timestamp=self._now(),
                source="system",
                input={"contract_snapshot": {"task_type": "screenplay.generate.stream"}},
            )
        )

    def _seed_tool_end(
        self,
        trace_id: str,
        tool_call_id: str,
        path: str,
        *,
        tool_output: dict | None = None,
    ) -> None:
        self._seed_event(
            TraceLogEvent(
                trace_id=trace_id,
                event_id=f"{trace_id}-{tool_call_id}-end",
                sequence=self._next_sequence(trace_id),
                type="tool_end",
                status="completed",
                timestamp=self._now(),
                source="middleware",
                agent_name="writing",
                tool_call_id=tool_call_id,
                tool_name="write_file",
                tool_args={"file_path": path, "content": "正文"},
                tool_output=tool_output,
            )
        )

    def _seed_revision(self, trace_id: str, tool_call_id: str, path: str, content: str) -> None:
        store = ContentAddressedPayloadStore(settings.trace_payload_path)
        payload_ref = store.put({"content": content})
        now = self._now()
        db.execute(
            """INSERT INTO payload_objects
               (payload_id, content_hash, kind, size_bytes, sensitivity, expires_at,
                storage_path, sealed, created_at)
               VALUES (?, ?, ?, ?, 'internal', NULL, ?, 1, ?)""",
            (
                payload_ref.payload_id,
                payload_ref.content_hash,
                payload_ref.kind,
                payload_ref.size_bytes,
                str(settings.trace_payload_path / f"{payload_ref.payload_id}.json"),
                now,
            ),
        )
        revision_id = f"revision-{trace_id}"
        event = TraceLogEvent(
            trace_id=trace_id,
            event_id=f"{trace_id}-revision",
            sequence=self._next_sequence(trace_id),
            type="artifact_revision",
            status="completed",
            timestamp=now,
            source="runtime",
            agent_name="writing",
            tool_call_id=tool_call_id,
            tool_name="write_file",
            artifact_revision_id=revision_id,
            artifact={
                "logical_key": path,
                "artifact_type": "workspace_file",
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            },
            payload_refs={"output": payload_ref},
        )
        self._seed_event(event)
        artifact_id = f"artifact-{trace_id}"
        db.execute(
            """INSERT INTO artifacts
               (artifact_id, artifact_type, workspace_id, logical_key, created_at)
               VALUES (?, 'workspace_file', 'ws', ?, ?)""",
            (artifact_id, path, now),
        )
        db.execute(
            """INSERT INTO artifact_revisions
               (artifact_revision_id, artifact_id, payload_id, content_hash,
                producer_trace_id, producer_event_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                revision_id,
                artifact_id,
                payload_ref.payload_id,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                trace_id,
                event.event_id,
                now,
            ),
        )

    def _seed_event(self, event: TraceLogEvent) -> None:
        payload_json = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        db.execute(
            """INSERT INTO event_payloads
               (trace_id, sequence, type, timestamp, payload_json, event_id,
                event_hash, payload_refs_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.trace_id,
                event.sequence,
                event.type,
                event.timestamp,
                payload_json,
                event.event_id,
                hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                json.dumps(
                    {key: ref.model_dump(mode="json") for key, ref in event.payload_refs.items()}
                ),
            ),
        )

    def _next_sequence(self, trace_id: str) -> int:
        row = db.query_one(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM event_payloads WHERE trace_id=?",
            (trace_id,),
        )
        return int(row["sequence"])

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    unittest.main()
