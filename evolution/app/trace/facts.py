"""Writer Trace V2 的完整性闸门、固定血缘和追加型质量事实。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import app.core.db as db
from app.core.settings import settings
from contracts.trace.payload import ContentAddressedPayloadStore


_OBJECT_TYPES = {
    "trace", "artifact_revision", "evidence_dossier", "evaluation_dossier",
    "score", "experiment", "candidate", "release",
}
_OUTCOME_TYPES = {
    "copy", "regenerate", "adopt", "edit_diff", "human_rating",
    "candidate_improved", "published", "rolled_back",
}
_RELEASE_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"committed"},
    "committed": {"registry_promoted"},
    "registry_promoted": {"executor_refresh_ack", "activation_failed"},
    "executor_refresh_ack": {"activated", "activation_failed"},
    "activated": {"rollback_activated"},
    "activation_failed": {"rollback_activated"},
    "rollback_activated": set(),
}


@dataclass(frozen=True)
class ConsumptionRejected(RuntimeError):
    consumer_workload: str
    source_type: str
    source_id: str
    integrity_status: str
    missing_fields: tuple[str, ...]

    def __str__(self) -> str:
        missing = ", ".join(self.missing_fields) or "unknown"
        return (
            f"{self.consumer_workload} rejected {self.source_type} {self.source_id}: "
            f"integrity={self.integrity_status}; missing={missing}"
        )


def require_verified_creation_trace(trace_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM runs WHERE trace_id=?", (trace_id,))
    if row is None:
        _reject("evidence_compile", "trace", trace_id, "unknown", ["trace"])
    missing: list[str] = []
    if int(row.get("schema_version") or 1) < 2:
        missing.append("schema_version")
    if row.get("workload") != "creation":
        missing.append("workload=creation")
    if row.get("integrity_status") != "verified":
        missing.append("integrity_status=verified")
    if missing:
        _reject(
            "evidence_compile", "trace", trace_id,
            str(row.get("integrity_status") or "unknown"), missing,
        )
    return row


def require_ready_evidence_dossier(dossier_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM evidence_dossiers WHERE pack_id=?", (dossier_id,))
    if row is None:
        _reject("evaluation", "evidence_dossier", dossier_id, "unknown", ["dossier"])
    missing = [
        field
        for field in ("manifest_json", "facts_json", "index_json")
        if not row.get(field)
    ]
    if row.get("status") != "ready":
        missing.append("status=ready")
    source = db.query_one(
        "SELECT integrity_status FROM runs WHERE trace_id=?", (row["trace_id"],)
    )
    if source is None or source.get("integrity_status") != "verified":
        missing.append("source_trace.integrity_status=verified")
    compile_trace_id = row.get("compile_trace_id")
    compile_run = (
        db.query_one(
            "SELECT integrity_status, workload FROM runs WHERE trace_id=?",
            (compile_trace_id,),
        )
        if compile_trace_id
        else None
    )
    if not compile_trace_id:
        missing.append("compile_trace_id")
    elif compile_run is None or compile_run.get("integrity_status") != "verified":
        missing.append("compile_trace.integrity_status=verified")
    elif compile_run.get("workload") != "evidence_compile":
        missing.append("compile_trace.workload=evidence_compile")
    if missing:
        _reject(
            "evaluation", "evidence_dossier", dossier_id,
            str((source or {}).get("integrity_status") or row.get("status") or "unknown"),
            missing,
        )
    return row


def require_sealed_evaluation_dossier(dossier_id: str) -> dict[str, Any]:
    row = db.query_one(
        """SELECT d.*, s.self_trace_id AS evaluation_trace_id
           FROM evaluation_dossiers d
           LEFT JOIN evaluation_sessions s ON s.eval_id=d.eval_attempt_id
           WHERE d.dossier_id=?""",
        (dossier_id,),
    )
    if row is None:
        _reject("evolution", "evaluation_dossier", dossier_id, "unknown", ["dossier"])
    missing: list[str] = []
    if row.get("seal_status") != "sealed":
        missing.append("seal_status=sealed")
    if row.get("completeness_status") != "complete":
        missing.append("completeness_status=complete")
    if not row.get("frozen_evidence_json"):
        missing.append("frozen_evidence")
    evaluation_trace_id = row.get("evaluation_trace_id")
    evaluation_run = (
        db.query_one(
            "SELECT integrity_status, workload FROM runs WHERE trace_id=?",
            (evaluation_trace_id,),
        )
        if evaluation_trace_id
        else None
    )
    if not evaluation_trace_id:
        missing.append("evaluation_trace_id")
    elif evaluation_run is None or evaluation_run.get("integrity_status") != "verified":
        missing.append("evaluation_trace.integrity_status=verified")
    elif evaluation_run.get("workload") != "evaluation":
        missing.append("evaluation_trace.workload=evaluation")
    if missing:
        _reject(
            "evolution", "evaluation_dossier", dossier_id,
            str(row.get("seal_status") or "unknown"), missing,
        )
    return row


def _reject(
    workload: str,
    source_type: str,
    source_id: str,
    integrity: str,
    missing: list[str],
) -> None:
    db.execute(
        """INSERT INTO consumption_rejections
           (consumer_workload, source_type, source_id, integrity_status,
            missing_fields_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (workload, source_type, source_id, integrity, json.dumps(missing), _now()),
    )
    raise ConsumptionRejected(
        workload, source_type, source_id, integrity, tuple(missing)
    )


def add_lineage(
    from_type: str, from_id: str, relation: str, to_type: str, to_id: str, *,
    conn: Any | None = None,
) -> None:
    if from_type not in _OBJECT_TYPES or to_type not in _OBJECT_TYPES:
        raise ValueError("unsupported Writer lineage object type")
    _execute(conn,
        """INSERT INTO lineage_edges
           (from_type, from_id, relation, to_type, to_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(from_type, from_id, relation, to_type, to_id) DO NOTHING""",
        (from_type, from_id, relation, to_type, to_id, _now()),
    )


def lineage_for(object_type: str, object_id: str) -> dict[str, list[dict[str, Any]]]:
    if object_type not in _OBJECT_TYPES:
        raise ValueError("unsupported Writer lineage object type")
    incoming = db.query_all(
        """SELECT * FROM lineage_edges WHERE to_type=? AND to_id=? ORDER BY id""",
        (object_type, object_id),
    )
    outgoing = db.query_all(
        """SELECT * FROM lineage_edges WHERE from_type=? AND from_id=? ORDER BY id""",
        (object_type, object_id),
    )
    return {"incoming": incoming, "outgoing": outgoing}


def append_outcome(
    *,
    target_type: str,
    target_id: str,
    outcome_type: str,
    actor_user_id: str | None,
    payload: Any | None = None,
    outcome_id: str | None = None,
) -> str:
    if target_type not in {"trace", "artifact_revision"}:
        raise ValueError("outcome target must be trace or artifact_revision")
    if outcome_type not in _OUTCOME_TYPES:
        raise ValueError("unsupported outcome_type")
    stable_id = outcome_id or f"outcome-{uuid4().hex}"
    payload_id = _store_payload(payload) if payload is not None else None
    existing = db.query_one("SELECT * FROM outcome_records WHERE outcome_id=?", (stable_id,))
    expected = (target_type, target_id, outcome_type, payload_id, actor_user_id)
    if existing is not None:
        actual = tuple(
            existing.get(key)
            for key in ("target_type", "target_id", "outcome_type", "payload_id", "actor_user_id")
        )
        if actual != expected:
            raise ValueError(f"outcome_id conflict: {stable_id}")
        return stable_id
    db.execute(
        """INSERT INTO outcome_records
           (outcome_id, target_type, target_id, outcome_type, payload_id,
            actor_user_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (*((stable_id,) + expected), _now()),
    )
    return stable_id


def append_score(
    *,
    target_type: str,
    target_id: str,
    rubric_id: str,
    rubric_version: str,
    score: dict[str, Any],
    actor_user_id: str,
    supersedes_score_id: str | None = None,
    score_id: str | None = None,
    conn: Any | None = None,
) -> str:
    stable_id = score_id or f"score-{uuid4().hex}"
    _execute(conn,
        """INSERT INTO score_records
           (score_id, target_type, target_id, rubric_id, rubric_version, score_json,
            supersedes_score_id, actor_user_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            stable_id, target_type, target_id, rubric_id, rubric_version,
            json.dumps(score, ensure_ascii=False, sort_keys=True), supersedes_score_id,
            actor_user_id, _now(),
        ),
    )
    add_lineage(target_type, target_id, "scored_by", "score", stable_id, conn=conn)
    return stable_id


def create_experiment(
    *,
    source_evaluation_dossier_id: str,
    candidate_revision_id: str,
    formula_version: str,
    baseline_revision_id: str | None = None,
    metrics: dict[str, Any] | None = None,
    status: str = "completed",
    experiment_id: str | None = None,
) -> str:
    stable_id = experiment_id or f"experiment-{uuid4().hex}"
    db.execute(
        """INSERT INTO experiment_runs_v2
           (experiment_id, source_evaluation_dossier_id, baseline_revision_id,
            candidate_revision_id, status, metrics_json, formula_version, created_at,
            finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            stable_id, source_evaluation_dossier_id, baseline_revision_id,
            candidate_revision_id, status,
            json.dumps(metrics or {}, ensure_ascii=False, sort_keys=True),
            formula_version, _now(), _now() if status == "completed" else None,
        ),
    )
    add_lineage("evaluation_dossier", source_evaluation_dossier_id, "compared_in", "experiment", stable_id)
    add_lineage("candidate", candidate_revision_id, "compared_in", "experiment", stable_id)
    return stable_id


def append_release_event(
    *,
    release_id: str,
    status: str,
    candidate_id: str | None,
    actor_user_id: str | None,
    release_event_id: str | None = None,
) -> str:
    prior = db.query_one(
        """SELECT status FROM release_events_v2 WHERE release_id=?
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (release_id,),
    )
    previous_status = prior.get("status") if prior else None
    if status not in _RELEASE_TRANSITIONS.get(previous_status, set()):
        raise ValueError(f"invalid release transition: {previous_status} -> {status}")
    stable_id = release_event_id or f"release-event-{uuid4().hex}"
    db.execute(
        """INSERT INTO release_events_v2
           (release_event_id, release_id, status, candidate_id, actor_user_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (stable_id, release_id, status, candidate_id, actor_user_id, _now()),
    )
    if status == "committed" and candidate_id:
        add_lineage("candidate", candidate_id, "selected_for", "release", release_id)
    return stable_id


def _store_payload(value: Any) -> str:
    ref = ContentAddressedPayloadStore(settings.trace_payload_path).put(value)
    db.execute(
        """INSERT INTO payload_objects
           (payload_id, content_hash, kind, size_bytes, sensitivity, expires_at,
            storage_path, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(payload_id) DO UPDATE SET expires_at=excluded.expires_at""",
        (
            ref.payload_id, ref.content_hash, ref.kind, ref.size_bytes, ref.sensitivity,
            ref.expires_at, str(settings.trace_payload_path / f"{ref.payload_id}.json"), _now(),
        ),
    )
    return ref.payload_id


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _execute(conn: Any | None, sql: str, params: tuple[Any, ...]) -> Any:
    return conn.execute(sql, params) if conn is not None else db.execute(sql, params)


__all__ = [
    "ConsumptionRejected", "require_verified_creation_trace",
    "require_ready_evidence_dossier", "require_sealed_evaluation_dossier",
    "add_lineage", "lineage_for", "append_outcome", "append_score",
    "create_experiment", "append_release_event",
]
