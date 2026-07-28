from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import app.core.db as db
from app.core.settings import settings
from app.trace.recorder import EvolutionTraceRecorder
from app.trace_payloads import read_payload
from contracts.trace import TraceSpanLink


class EvolutionTraceV2RecorderTest(unittest.TestCase):
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

    def test_records_canonical_v2_payload_link_and_verified_manifest(self) -> None:
        recorder = EvolutionTraceRecorder()
        handle = recorder.create_run(
            "eval-1",
            "evolution_eval",
            endpoint="eval-agent.run",
            workload="evaluation",
            links=[
                TraceSpanLink(
                    target_trace_id="trace-creation",
                    relation="consumes",
                    attributes={"dossier_id": "dossier-1", "dossier_version": 2},
                )
            ],
            external_refs={"evaluation_id": "eval-1", "dossier_id": "dossier-1"},
        )
        recorder.append_event(
            handle.trace_id,
            {
                "type": "run_meta",
                "status": "running",
                "source": "system",
                "input": {"prompt": "full evaluation prompt"},
            },
        )
        recorder.complete_run(handle.trace_id)

        run = db.query_one("SELECT * FROM runs WHERE trace_id=?", (handle.trace_id,))
        self.assertEqual(run["schema_version"], 2)
        self.assertEqual(run["service"], "evolution")
        self.assertEqual(run["workload"], "evaluation")
        self.assertEqual(run["integrity_status"], "verified")
        self.assertEqual(json.loads(run["links_json"])[0]["target_trace_id"], "trace-creation")
        external_refs = json.loads(run["external_refs_json"])
        self.assertEqual(external_refs["evaluation_id"], "eval-1")
        self.assertEqual(len(external_refs["w3c_trace_id"]), 32)
        self.assertEqual(len(external_refs["w3c_span_id"]), 16)
        self.assertTrue(external_refs["traceparent"].startswith("00-"))

        rows = db.query_all(
            "SELECT payload_json FROM event_payloads WHERE trace_id=? ORDER BY sequence",
            (handle.trace_id,),
        )
        event = next(json.loads(row["payload_json"]) for row in rows if json.loads(row["payload_json"])["type"] == "run_meta")
        self.assertIsNone(event.get("input"))
        payload_id = event["payload_refs"]["input"]["payload_id"]
        self.assertEqual(read_payload(payload_id), {"prompt": "full evaluation prompt"})
        receipt = db.query_one("SELECT * FROM trace_receipts WHERE trace_id=?", (handle.trace_id,))
        self.assertEqual(receipt["manifest_status"], "verified")


if __name__ == "__main__":
    unittest.main()
