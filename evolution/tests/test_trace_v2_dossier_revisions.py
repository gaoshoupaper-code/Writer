from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from contracts.trace import (
    TraceLogEvent,
    TraceManifest,
    TraceRunSummary,
    compute_trace_events_hash,
)
from contracts.trace.payload import ContentAddressedPayloadStore

import app.core.db as db
from app.core.settings import settings
from app.ingestion.importer import ingest_events


class TraceV2DossierRevisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = settings.evolution_db
        self.old_payload_dir = settings.trace_payload_dir
        self.old_workspace = settings.executor_workspace
        settings.evolution_db = str(Path(self.tmp.name) / "evolution.db")
        settings.trace_payload_dir = str(Path(self.tmp.name) / "payloads")
        settings.executor_workspace = str(Path(self.tmp.name) / "workspace")
        db._conn = None
        db.init_db()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        settings.evolution_db = self.old_db
        settings.trace_payload_dir = self.old_payload_dir
        settings.executor_workspace = self.old_workspace
        self.tmp.cleanup()

    def test_dossier_uses_final_immutable_revisions_after_workspace_changes(self) -> None:
        self._ingest_creation_trace()
        workspace = settings.executor_workspace_path / "user-1" / "workspace-1"
        (workspace / "chapter").mkdir(parents=True)
        (workspace / "review").mkdir(parents=True)
        (workspace / "demand.md").write_text("被后续工作区覆盖的需求", encoding="utf-8")
        (workspace / "chapter" / "001.md").write_text("被后续工作区覆盖的正文", encoding="utf-8")
        (workspace / "review" / "writing.md").write_text("被后续工作区覆盖的评审", encoding="utf-8")

        from app.dossier.extractor import extract_facts

        facts = extract_facts("trace-v2-dossier")

        self.assertEqual(facts["provenance"], "trace_time")
        self.assertEqual(facts["contract"]["demand_md"], "# 冻结需求\n写一部小说")
        chapter = facts["deliveries"]["writing"]["/chapter/001.md"]
        self.assertEqual(chapter["content_frozen"], "第二版冻结正文")
        self.assertEqual(chapter["artifact_revision_id"], "revision-chapter-2")
        self.assertEqual(
            facts["review_artifacts"]["/review/writing.md"],
            "W1 unresolved: 冻结评审意见",
        )
        self.assertEqual(len(facts["artifact_revisions"]), 4)
        self.assertNotIn("被后续工作区覆盖", str(facts))

    def test_dossier_rejects_materialized_revision_hash_mismatch(self) -> None:
        self._ingest_creation_trace()
        db.execute(
            "UPDATE artifact_revisions SET content_hash=? WHERE artifact_revision_id=?",
            ("0" * 64, "revision-chapter-2"),
        )

        from app.dossier.extractor import extract_facts

        with self.assertRaisesRegex(ValueError, "revision-chapter-2.*hash"):
            extract_facts("trace-v2-dossier")

    def test_legacy_workspace_extractor_rejects_v2_trace(self) -> None:
        self._ingest_creation_trace()

        from app.eval_agent.eval_extractor import extract_deliveries

        with self.assertRaisesRegex(RuntimeError, "immutable ArtifactRevision"):
            extract_deliveries("trace-v2-dossier")

    def _ingest_creation_trace(self) -> None:
        store = ContentAddressedPayloadStore(settings.trace_payload_path)
        payload_values: dict[str, object] = {}

        def payload(value: object):
            ref = store.put(value)
            payload_values[ref.payload_id] = value
            return ref

        contract = {
            "user_goal": "写一部小说",
            "task_type": "screenplay.generate.stream",
            "run_purpose": "user_generation",
            "endpoint": "screenplay.generate.stream",
            "thread_id": "thread-1",
            "workspace_id": "workspace-1",
            "session_name": "session-1",
            "demand_md": "# 冻结需求\n写一部小说",
            "missing": [],
        }
        events = [
            TraceLogEvent(
                trace_id="trace-v2-dossier",
                event_id="event-1",
                sequence=1,
                type="run_start",
                status="running",
                timestamp="2026-01-01T00:00:01+00:00",
                source="runtime",
                schema_version=2,
                input={
                    "workspace_id": "workspace-1",
                    "thread_id": "thread-1",
                    "session_name": "session-1",
                    "user_id": "user-1",
                },
            ),
            TraceLogEvent(
                trace_id="trace-v2-dossier",
                event_id="event-2",
                sequence=2,
                type="run_meta",
                status="running",
                timestamp="2026-01-01T00:00:02+00:00",
                source="system",
                schema_version=2,
                payload_refs={"input": payload({"contract_snapshot": contract})},
            ),
            self._artifact_event(
                3,
                "revision-demand",
                "/demand.md",
                "# 冻结需求\n写一部小说",
                "interview-subagent",
                payload,
            ),
            self._artifact_event(
                4,
                "revision-chapter-1",
                "/chapter/001.md",
                "第一版冻结正文",
                "writing-subagent",
                payload,
            ),
            self._artifact_event(
                5,
                "revision-chapter-2",
                "/chapter/001.md",
                "第二版冻结正文",
                "writing-subagent",
                payload,
                parent_revision_id="revision-chapter-1",
            ),
            self._artifact_event(
                6,
                "revision-review",
                "/review/writing.md",
                "W1 unresolved: 冻结评审意见",
                "writing-review-subagent",
                payload,
            ),
            TraceLogEvent(
                trace_id="trace-v2-dossier",
                event_id="event-7",
                sequence=7,
                type="run_end",
                status="completed",
                timestamp="2026-01-01T00:00:07+00:00",
                source="runtime",
                schema_version=2,
            ),
        ]
        run = TraceRunSummary(
            trace_id="trace-v2-dossier",
            workspace_id="workspace-1",
            thread_id="thread-1",
            session_name="session-1",
            workspace_path="",
            endpoint="screenplay.generate.stream",
            status="completed",
            started_at=events[0].timestamp,
            ended_at=events[-1].timestamp,
            event_count=len(events),
            path="",
            schema_version=2,
            service="executor",
            workload="creation",
            purpose="user_generation",
            integrity_status="incomplete",
            manifest=TraceManifest(
                trace_id="trace-v2-dossier",
                final_sequence=7,
                terminal_event_id="event-7",
                events_hash=compute_trace_events_hash(events),
                payload_ids=sorted(payload_values),
                created_at="2026-01-01T00:00:08+00:00",
            ),
        )

        ingest_events(
            events,
            workspace_id_hint="workspace-1",
            run_summary_hint=run,
            payload_values=payload_values,
        )
        stored = db.query_one(
            "SELECT integrity_status FROM runs WHERE trace_id='trace-v2-dossier'"
        )
        receipt = db.query_one(
            "SELECT manifest_status, missing_ranges_json FROM trace_receipts "
            "WHERE trace_id='trace-v2-dossier'"
        )
        persisted_events = [
            TraceLogEvent.model_validate(json.loads(row["payload_json"]))
            for row in db.query_all(
                "SELECT payload_json FROM event_payloads WHERE trace_id='trace-v2-dossier' ORDER BY sequence"
            )
        ]
        actual_ids = sorted(
            ref.payload_id for event in persisted_events for ref in event.payload_refs.values()
        )
        self.assertEqual(
            stored["integrity_status"],
            "verified",
            {
                "receipt": receipt,
                "expected_hash": run.manifest.events_hash if run.manifest else None,
                "actual_hash": compute_trace_events_hash(persisted_events),
                "expected_payload_ids": run.manifest.payload_ids if run.manifest else None,
                "actual_payload_ids": actual_ids,
                "event_differences": [
                    (expected.event_id, expected.model_dump(exclude_none=True), actual.model_dump(exclude_none=True))
                    for expected, actual in zip(events, persisted_events)
                    if expected.model_dump(exclude_none=True) != actual.model_dump(exclude_none=True)
                ],
            },
        )

    @staticmethod
    def _artifact_event(
        sequence: int,
        revision_id: str,
        logical_key: str,
        content: str,
        agent_name: str,
        payload,
        *,
        parent_revision_id: str | None = None,
    ) -> TraceLogEvent:
        output = {"content": content}
        return TraceLogEvent(
            trace_id="trace-v2-dossier",
            event_id=f"event-{sequence}",
            sequence=sequence,
            type="artifact_revision",
            status="completed",
            timestamp=f"2026-01-01T00:00:0{sequence}+00:00",
            source="runtime",
            schema_version=2,
            agent_name=agent_name,
            tool_name="write_file",
            artifact_revision_id=revision_id,
            artifact={
                "artifact_type": "workspace_file",
                "logical_key": logical_key,
                "parent_revision_id": parent_revision_id,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            },
            payload_refs={"output": payload(output)},
        )


if __name__ == "__main__":
    unittest.main()
