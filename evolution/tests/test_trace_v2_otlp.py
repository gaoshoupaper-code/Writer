from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import app.core.db as db
from app.core.settings import settings


class TraceV2OtlpProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = settings.evolution_db
        self.old_endpoint = settings.trace_otlp_endpoint
        settings.evolution_db = str(Path(self.tmp.name) / "evolution.db")
        settings.trace_otlp_endpoint = ""
        db._conn = None
        db.init_db()
        db.execute(
            """INSERT INTO runs
               (trace_id, workspace_id, status, started_at, ended_at, duration_ms,
                event_count, ingested_at, schema_version, service, workload,
                integrity_status, external_refs_json)
               VALUES ('trace-1', 'ws', 'completed', '2026-07-28T00:00:00+00:00',
                       '2026-07-28T00:00:01+00:00', 1000, 2,
                       '2026-07-28T00:00:01+00:00', 2, 'executor', 'creation',
                       'verified', ?)""",
            (json.dumps({
                "w3c_trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                "w3c_span_id": "00f067aa0ba902b7",
            }),),
        )
        db.execute(
            """INSERT INTO nodes
               (node_id, trace_id, kind, label, status, depth, started_at, ended_at,
                duration_ms, usage_input, usage_output, usage_total)
               VALUES ('node-1', 'trace-1', 'llm', 'private prompt label', 'completed', 1,
                       '2026-07-28T00:00:00.100000+00:00',
                       '2026-07-28T00:00:00.900000+00:00', 800, 10, 5, 15)"""
        )
        db.execute(
            """INSERT INTO event_payloads
               (trace_id, sequence, type, timestamp, payload_json)
               VALUES ('trace-1', 1, 'llm_start', '2026-07-28T00:00:00+00:00',
                       '{"prompt":"never export this prompt"}')"""
        )

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        settings.evolution_db = self.old_db
        settings.trace_otlp_endpoint = self.old_endpoint
        self.tmp.cleanup()

    def test_projection_contains_structure_without_semantic_payload(self) -> None:
        from app.trace.otlp import build_otlp_request

        payload = build_otlp_request("trace-1")

        self.assertIsNotNone(payload)
        serialized = json.dumps(payload)
        self.assertNotIn("never export this prompt", serialized)
        self.assertNotIn("private prompt label", serialized)
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["traceId"], "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(spans[0]["spanId"], "00f067aa0ba902b7")
        self.assertEqual(spans[1]["name"], "writer.node.llm")

    def test_disabled_export_does_not_schedule(self) -> None:
        from app.trace.otlp import schedule_otlp_export

        self.assertFalse(schedule_otlp_export("trace-1"))


if __name__ == "__main__":
    unittest.main()
