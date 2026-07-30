"""证据卷宗来源资格与业务证据完整性的单一判定入口。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import app.core.db as db
from app.core.models import TraceLogEvent
from app.dossier.tool_results import is_successful_tool_end
from app.trace_payloads import hydrate_event, read_payload


_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
_VERSIONED_ARTIFACT_PATHS = frozenset({
    "/demand.md",
    "/outline.md",
    "/storyline.md",
    "/worldview.md",
    "/novel.md",
})
_VERSIONED_ARTIFACT_PREFIXES = (
    "/character/",
    "/storyline/",
    "/detail/",
    "/chapter/",
    "/review/",
)


@dataclass(frozen=True)
class CreationTraceEligibility:
    trace_id: str
    row: dict[str, Any] | None
    eligible: bool
    evidence_status: str
    transport_integrity: str
    missing_fields: tuple[str, ...]
    successful_write_count: int = 0
    artifact_revision_count: int = 0


def assess_creation_trace(trace_id: str) -> CreationTraceEligibility:
    """同时判定创作来源资格、传输完整性和业务证据完整性。"""
    row = db.query_one("SELECT * FROM runs WHERE trace_id=?", (trace_id,))
    if row is None:
        return CreationTraceEligibility(
            trace_id=trace_id,
            row=None,
            eligible=False,
            evidence_status="unknown",
            transport_integrity="unknown",
            missing_fields=("trace",),
        )

    source_gaps: list[str] = []
    if int(row.get("schema_version") or 1) < 2:
        source_gaps.append("schema_version>=2")
    if row.get("service") != "executor":
        source_gaps.append("service=executor")
    if row.get("workload") != "creation":
        source_gaps.append("workload=creation")
    if row.get("status") != "completed":
        source_gaps.append("status=completed")
    transport_integrity = str(row.get("integrity_status") or "legacy")
    if transport_integrity != "verified":
        source_gaps.append("integrity_status=verified")

    if source_gaps:
        report = CreationTraceEligibility(
            trace_id=trace_id,
            row=row,
            eligible=False,
            evidence_status="ineligible_source",
            transport_integrity=transport_integrity,
            missing_fields=tuple(source_gaps),
        )
        _persist_report(report)
        return report

    evidence_gaps: list[str] = []
    events = _load_events(trace_id)
    if not _has_frozen_contract(events, trace_id):
        evidence_gaps.append("frozen_task_contract")

    successful_writes = _successful_versioned_writes(events)
    revisions = _load_revisions(trace_id)
    recovery_revisions = [
        revision for revision in revisions
        if revision.get("provenance") == "trace_payload_recovery"
    ]
    revision_keys: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for revision in revisions:
        tool_call_id = str(revision.get("tool_call_id") or "")
        logical_key = _normalize_path(revision.get("logical_key"))
        content_hash = str(revision.get("content_hash") or "")
        revision_id = str(revision.get("artifact_revision_id") or "")
        if not tool_call_id or not logical_key:
            evidence_gaps.append(f"artifact_revision_unlinked:{revision_id or 'unknown'}")
            continue
        revision_keys.setdefault((tool_call_id, logical_key), []).append(
            (content_hash, revision_id)
        )
        payload = read_payload(str(revision.get("payload_id") or ""))
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str):
            evidence_gaps.append(f"artifact_payload_unreadable:{revision_id}")
        elif hashlib.sha256(content.encode("utf-8")).hexdigest() != content_hash:
            evidence_gaps.append(f"artifact_payload_hash_mismatch:{revision_id}")

    missing_write_count = 0
    if recovery_revisions:
        recovered_paths = {
            _normalize_path(revision.get("logical_key")) for revision in recovery_revisions
        }
        expected_paths = {logical_key for _, logical_key in successful_writes}
        missing_write_count = len(expected_paths - recovered_paths)
    else:
        for tool_call_id, logical_key in successful_writes:
            matches = revision_keys.get((tool_call_id, logical_key), [])
            if not matches:
                missing_write_count += 1
                continue
            unique_hashes = {content_hash for content_hash, _ in matches}
            if len(matches) > 1 and len(unique_hashes) == 1:
                evidence_gaps.append(f"artifact_revision_duplicate:{tool_call_id}:{logical_key}")
            elif len(unique_hashes) > 1:
                evidence_gaps.append(f"artifact_revision_conflict:{tool_call_id}:{logical_key}")

    if missing_write_count:
        evidence_gaps.append(f"artifact_revision_missing:{missing_write_count}")
    if not successful_writes:
        evidence_gaps.append("successful_artifact_write")
    if not revisions:
        evidence_gaps.append("artifact_revision")

    missing_fields = tuple(dict.fromkeys(evidence_gaps))
    report = CreationTraceEligibility(
        trace_id=trace_id,
        row=row,
        eligible=not missing_fields,
        evidence_status="complete" if not missing_fields else "incomplete",
        transport_integrity=transport_integrity,
        missing_fields=missing_fields,
        successful_write_count=len(successful_writes),
        artifact_revision_count=len(revisions),
    )
    _persist_report(report)
    return report


def list_creation_trace_candidates(*, limit: int, offset: int) -> dict[str, Any]:
    """返回能被启动接口接受的创作 Trace；不维护第二套前端黑名单。"""
    rows = db.query_all(
        """SELECT r.*, uc.username AS owner_username
           FROM runs r
           LEFT JOIN user_cache uc ON r.owner_user_id=uc.user_id
           WHERE r.schema_version>=2 AND r.service='executor' AND r.workload='creation'
             AND r.status='completed' AND r.integrity_status='verified'
           ORDER BY r.started_at DESC"""
    )
    candidates: list[dict[str, Any]] = []
    for row in rows:
        report = assess_creation_trace(row["trace_id"])
        if not report.eligible:
            continue
        item = dict(row)
        item["coverage"] = json.loads(item.get("coverage_json") or "{}")
        item["evidence_status"] = report.evidence_status
        item["evidence_gaps"] = []
        item["flag_count"] = 0
        item["skill_activation_count"] = 0
        item["middleware_intervention_count"] = 0
        item["hitl_count"] = 0
        candidates.append(item)
    return {
        "items": candidates[offset:offset + limit],
        "total": len(candidates),
        "limit": limit,
        "offset": offset,
    }


def _load_events(trace_id: str) -> list[TraceLogEvent]:
    events: list[TraceLogEvent] = []
    for row in db.query_all(
        "SELECT payload_json FROM event_payloads WHERE trace_id=? ORDER BY sequence",
        (trace_id,),
    ):
        try:
            events.append(TraceLogEvent.model_validate(json.loads(row["payload_json"])))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return events


def _has_frozen_contract(events: list[TraceLogEvent], trace_id: str) -> bool:
    for event in events:
        if event.type != "run_meta":
            continue
        hydrated = hydrate_event(event)
        if isinstance(hydrated.input, dict) and isinstance(
            hydrated.input.get("contract_snapshot"), dict
        ):
            return True
    demand = db.query_one(
        """SELECT 1 AS found
           FROM artifact_revisions revision
           JOIN artifacts artifact ON artifact.artifact_id=revision.artifact_id
           WHERE (revision.producer_trace_id=? OR revision.source_trace_id=?)
             AND artifact.logical_key='/demand.md'
           LIMIT 1""",
        (trace_id, trace_id),
    )
    if demand is not None:
        return True
    recovery_events = db.query_all(
        """SELECT event.payload_json
           FROM lineage_edges edge
           JOIN event_payloads event ON event.trace_id=edge.from_id
           WHERE edge.from_type='trace' AND edge.relation='recovers'
             AND edge.to_type='trace' AND edge.to_id=? AND event.type='run_meta'""",
        (trace_id,),
    )
    for row in recovery_events:
        try:
            event = hydrate_event(TraceLogEvent.model_validate(json.loads(row["payload_json"])))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(event.input, dict) and isinstance(event.input.get("contract_snapshot"), dict):
            return True
    return False


def _successful_versioned_writes(events: list[TraceLogEvent]) -> list[tuple[str, str]]:
    writes: list[tuple[str, str]] = []
    for event in events:
        if (
            not is_successful_tool_end(event)
            or event.tool_name not in _WRITE_TOOLS
            or not event.tool_call_id
        ):
            continue
        hydrated = hydrate_event(event)
        args = hydrated.tool_args if isinstance(hydrated.tool_args, dict) else {}
        path = _normalize_path(args.get("file_path") or args.get("path"))
        if _is_versioned_artifact_path(path):
            writes.append((event.tool_call_id, path))
    return writes


def _load_revisions(trace_id: str) -> list[dict[str, Any]]:
    rows = db.query_all(
        """SELECT revision.artifact_revision_id, revision.payload_id,
                  revision.content_hash, revision.producer_event_id,
                  artifact.logical_key, revision.provenance, revision.source_trace_id,
                  event.payload_json
           FROM artifact_revisions revision
           JOIN artifacts artifact ON artifact.artifact_id=revision.artifact_id
           LEFT JOIN event_payloads event
             ON event.trace_id=revision.producer_trace_id
            AND event.event_id=revision.producer_event_id
           WHERE revision.producer_trace_id=? OR revision.source_trace_id=?""",
        (trace_id, trace_id),
    )
    revisions: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            event = TraceLogEvent.model_validate(json.loads(item.get("payload_json") or "{}"))
        except (json.JSONDecodeError, TypeError, ValueError):
            event = None
        item["tool_call_id"] = event.tool_call_id if event else None
        revisions.append(item)
    return revisions


def _persist_report(report: CreationTraceEligibility) -> None:
    if report.row is None:
        return
    columns = {row[1] for row in db.get_conn().execute("PRAGMA table_info(runs)").fetchall()}
    if "evidence_status" not in columns:
        return
    db.execute(
        "UPDATE runs SET evidence_status=?, evidence_gaps_json=? WHERE trace_id=?",
        (report.evidence_status, json.dumps(report.missing_fields), report.trace_id),
    )


def _normalize_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return "/" + value.replace("\\", "/").lstrip("/")


def _is_versioned_artifact_path(path: str) -> bool:
    return path in _VERSIONED_ARTIFACT_PATHS or path.startswith(_VERSIONED_ARTIFACT_PREFIXES)


__all__ = [
    "CreationTraceEligibility",
    "assess_creation_trace",
    "list_creation_trace_candidates",
]
