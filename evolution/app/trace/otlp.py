"""Canonical Writer Trace 到无正文 OTLP/HTTP JSON 的可选单向投影。"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from queue import Full, Queue
from threading import Lock, Thread
from typing import Any

import app.core.db as db
from app.core.settings import settings


logger = logging.getLogger("evolution.trace.otlp")
_FORMULA_VERSION = "writer-trace-v2/otlp-1"
_EXPORT_QUEUE: Queue[str] = Queue(maxsize=1000)
_WORKER_LOCK = Lock()
_WORKER_STARTED = False


def build_otlp_request(trace_id: str) -> dict[str, Any] | None:
    run = db.query_one("SELECT * FROM runs WHERE trace_id=?", (trace_id,))
    if run is None:
        return None
    nodes = db.query_all(
        """SELECT node_id, parent_node_id, kind, status, depth, started_at, ended_at,
                  duration_ms, usage_input, usage_output, usage_total
           FROM nodes WHERE trace_id=? ORDER BY depth, started_at, node_id""",
        (trace_id,),
    )
    external_refs = _json_object(run.get("external_refs_json"))
    otlp_trace_id = _valid_hex(external_refs.get("w3c_trace_id"), 32) or _hash_hex(trace_id, 32)
    root_span_id = _valid_hex(external_refs.get("w3c_span_id"), 16) or _hash_hex(trace_id, 16)
    root_start = _to_unix_nano(run.get("started_at"))
    root_end = _end_nanos(run.get("ended_at"), root_start, run.get("duration_ms"))
    spans = [{
        "traceId": otlp_trace_id,
        "spanId": root_span_id,
        "name": "writer.run",
        "kind": 1,
        "startTimeUnixNano": str(root_start),
        "endTimeUnixNano": str(root_end),
        "attributes": _attributes({
            "writer.trace_id": trace_id,
            "writer.schema_version": int(run.get("schema_version") or 1),
            "writer.service": run.get("service") or "unknown",
            "writer.workload": run.get("workload") or "unknown",
            "writer.integrity_status": run.get("integrity_status") or "legacy",
            "writer.run.status": run.get("status") or "unknown",
            "writer.event_count": int(run.get("event_count") or 0),
        }),
        "status": _span_status(str(run.get("status") or "")),
    }]
    span_ids = {
        str(node["node_id"]): _hash_hex(f"{trace_id}:{node['node_id']}", 16)
        for node in nodes
    }
    for node in nodes:
        node_start = _to_unix_nano(node.get("started_at"), default=root_start)
        node_end = _end_nanos(node.get("ended_at"), node_start, node.get("duration_ms"))
        parent_span_id = span_ids.get(str(node.get("parent_node_id") or ""), root_span_id)
        spans.append({
            "traceId": otlp_trace_id,
            "spanId": span_ids[str(node["node_id"])],
            "parentSpanId": parent_span_id,
            "name": f"writer.node.{node.get('kind') or 'unknown'}",
            "kind": 1,
            "startTimeUnixNano": str(node_start),
            "endTimeUnixNano": str(node_end),
            "attributes": _attributes({
                "writer.node.kind": node.get("kind") or "unknown",
                "writer.node.status": node.get("status") or "unknown",
                "writer.node.depth": int(node.get("depth") or 0),
                "gen_ai.usage.input_tokens": int(node.get("usage_input") or 0),
                "gen_ai.usage.output_tokens": int(node.get("usage_output") or 0),
                "gen_ai.usage.total_tokens": int(node.get("usage_total") or 0),
            }),
            "status": _span_status(str(node.get("status") or "")),
        })
    return {
        "resourceSpans": [{
            "resource": {"attributes": _attributes({
                "service.name": f"writer.{run.get('service') or 'unknown'}",
                "writer.projection.version": _FORMULA_VERSION,
            })},
            "scopeSpans": [{
                "scope": {"name": "writer.trace.otlp-projection", "version": _FORMULA_VERSION},
                "spans": spans,
            }],
        }],
    }


def schedule_otlp_export(trace_id: str) -> bool:
    if not settings.trace_otlp_endpoint:
        return False
    _ensure_worker()
    try:
        _EXPORT_QUEUE.put_nowait(trace_id)
    except Full:
        logger.warning("OTLP 导出队列已满，丢弃派生投影 trace=%s", trace_id)
        return False
    return True


def export_otlp_trace(trace_id: str) -> bool:
    endpoint = settings.trace_otlp_endpoint
    if not endpoint:
        return False
    payload = build_otlp_request(trace_id)
    if payload is None:
        return False
    headers = {"Content-Type": "application/json"}
    try:
        configured_headers = json.loads(settings.trace_otlp_headers_json or "{}")
        if not isinstance(configured_headers, dict):
            raise ValueError("TRACE_OTLP_HEADERS_JSON must contain an object")
        headers.update({str(key): str(value) for key, value in configured_headers.items()})
        import httpx

        response = httpx.post(
            endpoint,
            json=payload,
            headers=headers,
            timeout=settings.trace_otlp_timeout_seconds,
        )
        response.raise_for_status()
        return True
    except Exception:
        logger.exception("OTLP 派生投影失败 trace=%s", trace_id)
        return False


def _ensure_worker() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        Thread(target=_worker_loop, name="writer-otlp-export", daemon=True).start()
        _WORKER_STARTED = True


def _worker_loop() -> None:
    while True:
        trace_id = _EXPORT_QUEUE.get()
        try:
            export_otlp_trace(trace_id)
        finally:
            _EXPORT_QUEUE.task_done()


def _attributes(values: dict[str, str | int]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key, value in values.items():
        encoded = {"intValue": str(value)} if isinstance(value, int) else {"stringValue": value}
        result.append({"key": key, "value": encoded})
    return result


def _span_status(status: str) -> dict[str, int]:
    if status == "failed":
        return {"code": 2}
    if status == "completed":
        return {"code": 1}
    return {"code": 0}


def _to_unix_nano(value: Any, *, default: int = 0) -> int:
    if not value:
        return default
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1_000_000_000)
    except ValueError:
        return default


def _end_nanos(value: Any, start: int, duration_ms: Any) -> int:
    parsed = _to_unix_nano(value)
    if parsed:
        return max(start, parsed)
    return start + max(0, int(duration_ms or 0)) * 1_000_000


def _valid_hex(value: Any, length: int) -> str | None:
    text = str(value or "").lower()
    if len(text) != length:
        return None
    try:
        return text if int(text, 16) else None
    except ValueError:
        return None


def _hash_hex(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _json_object(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


__all__ = ["build_otlp_request", "schedule_otlp_export", "export_otlp_trace"]
