from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("EXECUTOR_URL", "http://127.0.0.1:0")

from contracts.trace import (  # noqa: E402
    TraceLogEvent,
    TraceManifest,
    TracePayloadRef,
    TraceRunSummary,
    TraceSpanLink,
)

import app.core.db as db  # noqa: E402
from app.core.settings import settings  # noqa: E402
from app.ingestion.importer import ingest_events  # noqa: E402
from app.trace_payloads import delete_trace_payloads, purge_expired_payloads  # noqa: E402
from app.view.traces import _audit_content_access, _require_full_content_access  # noqa: E402
from contracts.trace.payload import ContentAddressedPayloadStore  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _event(
    sequence: int,
    event_type: str,
    *,
    payload_ref: TracePayloadRef | None = None,
) -> TraceLogEvent:
    return TraceLogEvent(
        trace_id="trace-v2-ingestion",
        event_id=f"event-{sequence}",
        sequence=sequence,
        type=event_type,
        status="completed" if event_type == "run_end" else "running",
        timestamp=f"2026-01-01T00:00:0{sequence}+00:00",
        source="runtime",
        schema_version=2,
        payload_refs={"input": payload_ref} if payload_ref else {},
    )


class TraceV2IngestionTest(unittest.TestCase):
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

    def test_missing_one_of_multiple_payloads_keeps_trace_incomplete(self) -> None:
        refs = [
            TracePayloadRef(
                payload_id=f"{'a' if index == 0 else 'b'}" * 64,
                content_hash=f"{'a' if index == 0 else 'b'}" * 64,
                kind="semantic_full",
                size_bytes=10,
            )
            for index in range(2)
        ]
        events = [
            _event(1, "run_start"),
            _event(2, "llm_start", payload_ref=refs[0]),
            _event(3, "llm_end", payload_ref=refs[1]),
            _event(4, "run_end"),
        ]
        run = self._run(events, terminal_event_id="event-4")

        ingest_events(events, run_summary_hint=run, payload_values={refs[0].payload_id: "one"})

        row = db.query_one(
            "SELECT integrity_status FROM runs WHERE trace_id=?", (run.trace_id,)
        )
        self.assertEqual(row["integrity_status"], "incomplete")

    def test_legacy_row_without_hash_reingests_idempotently(self) -> None:
        legacy = TraceLogEvent(
            trace_id="legacy-trace",
            event_id="legacy-event",
            sequence=1,
            type="run_start",
            status="running",
            timestamp="2026-01-01T00:00:00+00:00",
            source="system",
        )
        ingest_events([legacy])
        db.execute(
            "UPDATE event_payloads SET event_hash=NULL WHERE trace_id='legacy-trace'"
        )

        ingest_events([legacy])

        conflicts = db.query_one(
            "SELECT COUNT(*) AS count FROM integrity_conflicts WHERE trace_id='legacy-trace'"
        )
        self.assertEqual(conflicts["count"], 0)

    def test_conflict_is_sticky_and_never_enters_canonical_projection(self) -> None:
        original = [
            _event(1, "run_start"),
            _event(2, "run_end"),
        ]
        run = self._run(original, terminal_event_id="event-2")
        ingest_events(original, run_summary_hint=run)
        conflicting_start = original[0].model_copy(update={"node_name": "forged-node"})

        ingest_events([conflicting_start, original[1]], run_summary_hint=run)

        stored = db.query_one(
            "SELECT payload_json FROM event_payloads WHERE trace_id=? AND sequence=1",
            (run.trace_id,),
        )
        self.assertIsNone(json.loads(stored["payload_json"]).get("node_name"))
        row = db.query_one(
            "SELECT integrity_status FROM runs WHERE trace_id=?", (run.trace_id,)
        )
        self.assertEqual(row["integrity_status"], "conflict")

        ingest_events(original, run_summary_hint=run)

        row = db.query_one(
            "SELECT integrity_status FROM runs WHERE trace_id=?", (run.trace_id,)
        )
        self.assertEqual(row["integrity_status"], "conflict")

    def test_run_purpose_and_span_links_survive_ingestion(self) -> None:
        events = [_event(1, "run_start"), _event(2, "run_end")]
        run = self._run(events, terminal_event_id="event-2")
        run.purpose = "optimization"
        run.links = [TraceSpanLink(
            target_trace_id="trace-source",
            relation="triggered_by",
            attributes={"artifact_revision_id": "revision-1"},
        )]

        ingest_events(events, run_summary_hint=run)

        row = db.query_one(
            "SELECT run_purpose, links_json FROM runs WHERE trace_id=?", (run.trace_id,)
        )
        self.assertEqual(row["run_purpose"], "optimization")
        self.assertEqual(json.loads(row["links_json"])[0]["target_trace_id"], "trace-source")

    def test_missing_artifact_payload_never_materializes_revision(self) -> None:
        missing_ref = TracePayloadRef(
            payload_id="c" * 64,
            content_hash="c" * 64,
            kind="semantic_full",
            size_bytes=20,
        )
        events = [
            _event(1, "run_start"),
            _event(2, "artifact_revision", payload_ref=missing_ref).model_copy(update={
                "payload_refs": {"output": missing_ref},
                "artifact_revision_id": "revision-missing",
                "artifact": {
                    "artifact_type": "draft",
                    "logical_key": "chapter/1.md",
                    "content_hash": missing_ref.content_hash,
                },
            }),
            _event(3, "run_end"),
        ]
        run = self._run(events, terminal_event_id="event-3")

        ingest_events(events, run_summary_hint=run, payload_values={})

        revision = db.query_one(
            "SELECT 1 AS found FROM artifact_revisions WHERE artifact_revision_id=?",
            ("revision-missing",),
        )
        integrity = db.query_one(
            "SELECT integrity_status FROM runs WHERE trace_id=?", (run.trace_id,)
        )
        self.assertIsNone(revision)
        self.assertEqual(integrity["integrity_status"], "incomplete")

    def test_full_content_requires_super_admin_and_is_audited(self) -> None:
        denied = SimpleNamespace(
            state=SimpleNamespace(user_id="quality-user", is_super_admin=False)
        )
        with self.assertRaises(HTTPException) as raised:
            _require_full_content_access(denied)
        self.assertEqual(raised.exception.status_code, 403)

        allowed = SimpleNamespace(
            state=SimpleNamespace(user_id="admin-user", is_super_admin=True)
        )
        _require_full_content_access(allowed)
        _audit_content_access(allowed, "view", "trace", "trace-v2-ingestion")
        row = db.query_one(
            "SELECT actor_user_id, action FROM access_audit ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(row, {"actor_user_id": "admin-user", "action": "view"})

    def test_deleting_one_trace_preserves_shared_payload(self) -> None:
        store = ContentAddressedPayloadStore(settings.trace_payload_path)
        ref = store.put({"content": "shared"})
        now = "2026-01-01T00:00:00+00:00"
        for trace_id in ("trace-a", "trace-b"):
            db.execute(
                "INSERT INTO runs(trace_id, workspace_id, status, ingested_at) VALUES(?,?,?,?)",
                (trace_id, "workspace", "completed", now),
            )
        db.execute(
            """INSERT INTO payload_objects
               (payload_id, content_hash, kind, size_bytes, sensitivity, expires_at, storage_path, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                ref.payload_id,
                ref.content_hash,
                ref.kind,
                ref.size_bytes,
                ref.sensitivity,
                ref.expires_at,
                str(settings.trace_payload_path / f"{ref.payload_id}.json"),
                now,
            ),
        )
        for trace_id in ("trace-a", "trace-b"):
            db.execute(
                "INSERT INTO trace_payload_links(trace_id, event_id, field_name, payload_id) VALUES(?,?,?,?)",
                (trace_id, "event-1", "input", ref.payload_id),
            )

        delete_trace_payloads("trace-a")

        self.assertEqual(store.get(ref.payload_id), {"content": "shared"})
        remaining = db.query_one(
            "SELECT COUNT(*) AS count FROM trace_payload_links WHERE payload_id=?",
            (ref.payload_id,),
        )
        self.assertEqual(remaining["count"], 1)

    def test_retention_never_purges_sealed_payload(self) -> None:
        store = ContentAddressedPayloadStore(settings.trace_payload_path)
        ref = store.put({"content": "sealed"})
        db.execute(
            """INSERT INTO payload_objects
               (payload_id, content_hash, kind, size_bytes, sensitivity, expires_at,
                storage_path, sealed, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                ref.payload_id,
                ref.content_hash,
                ref.kind,
                ref.size_bytes,
                ref.sensitivity,
                "2020-01-01T00:00:00+00:00",
                str(settings.trace_payload_path / f"{ref.payload_id}.json"),
                1,
                "2020-01-01T00:00:00+00:00",
            ),
        )

        self.assertEqual(purge_expired_payloads(), 0)
        self.assertEqual(store.get(ref.payload_id), {"content": "sealed"})

    def test_llm_chain_summary_backfilled_from_payload_before_projection(self) -> None:
        """FR-004 / AC-001：llm_end 的 output 被 payload 外置时，投影 chain_summary
        必须基于回填后的正文，不得持久化为"无输出"。"""
        store = ContentAddressedPayloadStore(settings.trace_payload_path)
        llm_output = {"messages": [{"type": "ai", "content": "主角决定离开小镇去寻剑。"}]}
        output_ref_payload = store.put(llm_output)
        output_ref = TracePayloadRef(
            payload_id=output_ref_payload.payload_id,
            content_hash=output_ref_payload.content_hash,
            kind="semantic_full",
            size_bytes=output_ref_payload.size_bytes,
        )
        events = [
            _event(1, "run_start"),
            _event(2, "llm_start"),
            _event(3, "llm_end").model_copy(update={
                "payload_refs": {"output": output_ref},
                "model_name": "deepseek-chat",
            }),
            _event(4, "run_end"),
        ]
        run = self._run(events, terminal_event_id="event-4")

        ingest_events(
            events, run_summary_hint=run,
            payload_values={output_ref.payload_id: llm_output},
        )

        node = db.query_one(
            "SELECT chain_summary, kind FROM nodes WHERE trace_id=? AND kind='llm'",
            (run.trace_id,),
        )
        self.assertIsNotNone(node)
        self.assertNotIn("无输出", node["chain_summary"])
        self.assertIn("主角决定离开小镇", node["chain_summary"])

    def test_payload_backfill_missing_degrades_chain_summary_without_crash(self) -> None:
        """EDGE-003 / AC-002：payload_refs 指向的对象缺失时，chain_summary 降级保留
        可恢复信息（模型名/工具名），不静默"无输出"也不崩溃。"""
        missing_ref = TracePayloadRef(
            payload_id="d" * 64,
            content_hash="d" * 64,
            kind="semantic_full",
            size_bytes=10,
        )
        events = [
            _event(1, "run_start"),
            _event(2, "llm_start"),
            _event(3, "llm_end").model_copy(update={
                "payload_refs": {"output": missing_ref},
                "model_name": "deepseek-chat",
            }),
            _event(4, "run_end"),
        ]
        run = self._run(events, terminal_event_id="event-4")

        # 不提供 payload_values，payload_objects 也不会登记该 ref → 回填缺失
        ingest_events(events, run_summary_hint=run, payload_values={})

        node = db.query_one(
            "SELECT chain_summary, kind FROM nodes WHERE trace_id=? AND kind='llm'",
            (run.trace_id,),
        )
        self.assertIsNotNone(node)
        # 不崩溃；chain_summary 仍带模型名（可恢复信息），降级为"无输出"是可接受底线
        self.assertIn("deepseek-chat", node["chain_summary"])

    @staticmethod
    def _run(events: list[TraceLogEvent], terminal_event_id: str) -> TraceRunSummary:
        return TraceRunSummary(
            trace_id=events[0].trace_id,
            workspace_id="workspace",
            thread_id="thread",
            session_name="session",
            workspace_path="",
            endpoint="generate",
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
                trace_id=events[0].trace_id,
                final_sequence=events[-1].sequence,
                terminal_event_id=terminal_event_id,
                events_hash="not-yet-verified-in-this-slice",
                payload_ids=[
                    ref.payload_id for event in events for ref in event.payload_refs.values()
                ],
                created_at="2026-01-01T00:00:05+00:00",
            ),
        )


if __name__ == "__main__":
    unittest.main()
