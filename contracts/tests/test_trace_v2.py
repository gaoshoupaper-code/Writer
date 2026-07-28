from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from contracts.trace import TraceLogEvent, TraceRunSummary, compute_trace_events_hash
from contracts.trace.payload import (
    ContentAddressedPayloadStore,
    PayloadRejected,
    sanitize_structural_text,
)
from contracts.trace.w3c import create_trace_context, parse_traceparent


class TraceV2ContractTest(unittest.TestCase):
    def test_v1_defaults_remain_legacy_and_unknown(self) -> None:
        run = TraceRunSummary(
            trace_id="legacy-trace",
            workspace_id="workspace",
            thread_id="thread",
            session_name="session",
            workspace_path="",
            endpoint="generate",
            status="completed",
            started_at="2026-01-01T00:00:00+00:00",
            event_count=1,
            path="trace.jsonl",
        )

        self.assertEqual(run.schema_version, 1)
        self.assertEqual(run.integrity_status, "legacy")
        self.assertIsNone(run.workload)
        self.assertEqual(run.coverage, {})

    def test_v2_event_accepts_explicit_payload_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContentAddressedPayloadStore(Path(tmpdir))
            ref = store.put({"prompt": "完整正文"})
            event = TraceLogEvent(
                trace_id="trace-v2",
                event_id="event-1",
                sequence=1,
                type="llm_start",
                status="running",
                timestamp="2026-01-01T00:00:00+00:00",
                source="runtime",
                schema_version=2,
                payload_refs={"input": ref},
            )

            self.assertEqual(event.payload_refs["input"].content_hash, ref.payload_id)
            self.assertEqual(store.get(ref.payload_id), {"prompt": "完整正文"})

    def test_event_digest_is_stable_across_json_key_reordering(self) -> None:
        base = {
            "trace_id": "trace-v2",
            "event_id": "event-1",
            "sequence": 1,
            "type": "run_start",
            "status": "running",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "source": "runtime",
            "schema_version": 2,
        }
        first = TraceLogEvent(**base, input={"workspace_id": "ws", "user_id": "u1"})
        second = TraceLogEvent(**base, input={"user_id": "u1", "workspace_id": "ws"})

        self.assertEqual(compute_trace_events_hash([first]), compute_trace_events_hash([second]))

    def test_structural_text_redacts_secret_assignments(self) -> None:
        sanitized = sanitize_structural_text(
            "upstream failed: Authorization: Bearer sk-1234567890abcdefghijklmnop"
        )

        self.assertEqual(sanitized, "upstream failed: Authorization=[redacted]")


class PayloadGateTest(unittest.TestCase):
    def test_preserves_long_semantic_content_without_truncation(self) -> None:
        content = "开" + "正文" * 20_000 + "终"
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContentAddressedPayloadStore(Path(tmpdir))
            ref = store.put({"content": content})

            self.assertEqual(store.get(ref.payload_id)["content"], content)

    def test_rejects_forbidden_fields_secrets_and_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContentAddressedPayloadStore(Path(tmpdir))
            rejected = (
                {"Authorization": "Bearer value"},
                {"value": "sk-abcdefghijklmnopqrstuvwxyz"},
                {"content": b"binary"},
                {"reasoning": "private chain of thought"},
            )

            for value in rejected:
                with self.subTest(value=value), self.assertRaises(PayloadRejected):
                    store.put(value)

            self.assertEqual(list(Path(tmpdir).glob("*.json")), [])


class W3CTraceContextTest(unittest.TestCase):
    def test_valid_parent_keeps_w3c_trace_id_and_creates_local_span(self) -> None:
        incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

        context = create_trace_context(incoming)

        self.assertEqual(context.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(context.parent_span_id, "00f067aa0ba902b7")
        self.assertEqual(len(context.span_id), 16)
        self.assertEqual(context.traceparent, f"00-{context.trace_id}-{context.span_id}-01")
        self.assertEqual(context.external_refs["traceparent"], context.traceparent)

    def test_invalid_or_zero_parent_is_not_propagated(self) -> None:
        self.assertIsNone(parse_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01"))
        self.assertIsNone(parse_traceparent("not-a-traceparent"))

        context = create_trace_context("not-a-traceparent")
        self.assertIsNone(context.parent_span_id)
        self.assertEqual(len(context.trace_id), 32)
        self.assertEqual(len(context.span_id), 16)


if __name__ == "__main__":
    unittest.main()
