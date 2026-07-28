"""Trace V2 本地容量基准；独立运行，不进入常规 unittest。"""

from __future__ import annotations

import json
import math
import tempfile
import time
from pathlib import Path

import app.core.db as db
from app.core.settings import settings
from app.trace_payloads import read_payload
from app.view.traces import get_trace, list_traces
from contracts.trace.payload import ContentAddressedPayloadStore


RUN_COUNT = 100_000
SPAN_COUNT = 2_000
ITERATIONS = 20


def _p95_ms(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000


def _measure(call) -> float:
    samples: list[float] = []
    for _ in range(ITERATIONS):
        started = time.perf_counter()
        call()
        samples.append(time.perf_counter() - started)
    return _p95_ms(samples)


def _seed() -> tuple[str, str, str]:
    detail_trace_id = "trace-benchmark-detail"
    started_at = "2026-07-28T00:00:00+00:00"
    coverage_json = json.dumps({"payload": "known", "token": "known", "cost": "known"})
    run_rows = [
        (
            f"trace-benchmark-{index:06d}", "ws", "completed", started_at,
            1000, 3, started_at, 2, "executor", "creation", "verified", coverage_json,
        )
        for index in range(RUN_COUNT - 1)
    ]
    run_rows.append((
        detail_trace_id, "ws", "completed", started_at, 1000, SPAN_COUNT + 1,
        started_at, 2, "executor", "creation", "verified", coverage_json,
    ))
    with db.transaction() as conn:
        conn.executemany(
            """INSERT INTO runs
               (trace_id, workspace_id, status, started_at, duration_ms, event_count,
                ingested_at, schema_version, service, workload, integrity_status,
                coverage_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            run_rows,
        )
        event_rows = []
        run_start = {
            "trace_id": detail_trace_id,
            "event_id": f"{detail_trace_id}-1",
            "sequence": 1,
            "type": "run_start",
            "status": "running",
            "timestamp": started_at,
            "source": "system",
            "schema_version": 2,
        }
        event_rows.append((detail_trace_id, 1, "run_start", started_at, json.dumps(run_start), run_start["event_id"]))
        for index in range(SPAN_COUNT):
            sequence = index + 2
            event = {
                "trace_id": detail_trace_id,
                "event_id": f"{detail_trace_id}-{sequence}",
                "sequence": sequence,
                "type": "tool_start",
                "status": "running",
                "timestamp": started_at,
                "source": "runtime",
                "schema_version": 2,
                "run_id": f"tool-run-{index}",
                "span_id": f"tool-run-{index}",
                "tool_name": "read_file",
            }
            event_rows.append((detail_trace_id, sequence, "tool_start", started_at, json.dumps(event), event["event_id"]))
        conn.executemany(
            """INSERT INTO event_payloads
               (trace_id, sequence, type, timestamp, payload_json, event_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            event_rows,
        )

    long_content = "正文" * 500_000
    ref = ContentAddressedPayloadStore(settings.trace_payload_path).put({"content": long_content})
    db.execute(
        """INSERT INTO payload_objects
           (payload_id, content_hash, kind, size_bytes, sensitivity, expires_at,
            storage_path, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ref.payload_id, ref.content_hash, ref.kind, ref.size_bytes, ref.sensitivity,
            ref.expires_at, str(settings.trace_payload_path / f"{ref.payload_id}.json"), started_at,
        ),
    )
    return detail_trace_id, ref.payload_id, long_content


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        old_db = settings.evolution_db
        old_payload_dir = settings.trace_payload_dir
        try:
            settings.evolution_db = str(Path(tmpdir) / "evolution.db")
            settings.trace_payload_dir = str(Path(tmpdir) / "payloads")
            db._conn = None
            db.init_db()
            trace_id, payload_id, expected_content = _seed()

            def list_query() -> None:
                response = list_traces(
                    workspace=None, thread_id=None, status="completed", owner=None,
                    run_purpose=None, workload="creation", integrity_status="verified",
                    since="2026-07-01T00:00:00+00:00", until=None, limit=50, offset=0,
                )
                if response.total != RUN_COUNT or len(response.items) != 50:
                    raise AssertionError("list result mismatch")

            detail_node_count = 0

            def detail_query() -> None:
                nonlocal detail_node_count
                detail = get_trace(trace_id)
                detail_node_count = len(detail.nodes)

            list_query()
            detail_query()
            list_p95 = _measure(list_query)
            detail_p95 = _measure(detail_query)
            payload_p95 = _measure(lambda: read_payload(payload_id))
            restored = read_payload(payload_id)

            result = {
                "runs": RUN_COUNT,
                "spans": detail_node_count,
                "payload_bytes": len(expected_content.encode("utf-8")),
                "list_p95_ms": round(list_p95, 2),
                "structure_detail_p95_ms": round(detail_p95, 2),
                "payload_read_p95_ms": round(payload_p95, 2),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if detail_node_count < SPAN_COUNT:
                raise AssertionError(f"expected {SPAN_COUNT} projected spans, got {detail_node_count}")
            if restored != {"content": expected_content}:
                raise AssertionError("long payload round-trip mismatch")
            if list_p95 > 1000:
                raise AssertionError(f"list p95 budget exceeded: {list_p95:.2f}ms")
            if detail_p95 > 2000:
                raise AssertionError(f"detail p95 budget exceeded: {detail_p95:.2f}ms")
        finally:
            if db._conn is not None:
                db._conn.close()
            db._conn = None
            settings.evolution_db = old_db
            settings.trace_payload_dir = old_payload_dir


if __name__ == "__main__":
    main()
