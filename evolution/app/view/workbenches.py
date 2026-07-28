"""运行观测、固定血缘和 workload Profile 的共享查询底座。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

import app.core.db as db
from app.trace.access import audit_content_access, require_full_content_access
from app.trace.facts import lineage_for
from app.trace_payloads import read_payload


router = APIRouter(tags=["trace-workbenches"])
_WORKLOADS = ("creation", "evidence_compile", "evaluation", "evolution")
_FORMULA_VERSION = "writer-trace-v2/profile-1"
_ADVANCED_MIN_SAMPLE = 30


@router.get("/lineage/{object_type}/{object_id}")
def get_lineage(object_type: str, object_id: str) -> dict[str, Any]:
    try:
        graph = lineage_for(object_type, object_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"object": {"type": object_type, "id": object_id}, **graph}


@router.get("/artifacts/revisions/{revision_id}")
def get_artifact_revision(revision_id: str) -> dict[str, Any]:
    row = db.query_one(
        """SELECT r.*, a.artifact_type, a.workspace_id, a.logical_key,
                  p.kind AS payload_kind, p.size_bytes, p.sensitivity, p.expires_at
           FROM artifact_revisions r
           JOIN artifacts a ON a.artifact_id=r.artifact_id
           JOIN payload_objects p ON p.payload_id=r.payload_id
           WHERE r.artifact_revision_id=?""",
        (revision_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="ArtifactRevision not found")
    return dict(row)


@router.get("/traces/{trace_id}/artifact-revisions")
def list_trace_artifact_revisions(trace_id: str) -> dict[str, Any]:
    if db.query_one("SELECT 1 AS found FROM runs WHERE trace_id=?", (trace_id,)) is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    rows = db.query_all(
        """SELECT r.artifact_revision_id, r.artifact_id, r.parent_revision_id,
                  r.content_hash, r.producer_trace_id, r.producer_event_id,
                  r.harness_version, r.created_at, a.artifact_type, a.logical_key,
                  p.kind AS payload_kind, p.size_bytes, p.sensitivity, p.expires_at
           FROM artifact_revisions r
           JOIN artifacts a ON a.artifact_id=r.artifact_id
           JOIN payload_objects p ON p.payload_id=r.payload_id
           WHERE r.producer_trace_id=?
           ORDER BY r.created_at, r.artifact_revision_id""",
        (trace_id,),
    )
    return {"trace_id": trace_id, "items": rows, "total": len(rows)}


@router.get("/artifacts/revisions/{revision_id}/content")
def get_artifact_revision_content(revision_id: str, request: Request) -> dict[str, Any]:
    require_full_content_access(request)
    revision = get_artifact_revision(revision_id)
    content = read_payload(revision["payload_id"])
    if content is None:
        raise HTTPException(status_code=404, detail="ArtifactRevision content expired")
    audit_content_access(request, "view", "artifact_revision", revision_id)
    return {"artifact_revision_id": revision_id, "content": content}


@router.get("/analysis/profiles")
def workload_profiles(
    hours: int | None = Query(720, ge=1, le=8760),
) -> dict[str, Any]:
    started_after = (
        (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        if hours is not None
        else None
    )
    where = "schema_version>=2 AND workload IN ('creation','evidence_compile','evaluation','evolution')"
    params: tuple[Any, ...] = ()
    if started_after:
        where += " AND started_at>=?"
        params = (started_after,)
    runs = db.query_all(
        f"""SELECT trace_id, workload, status, duration_ms, integrity_status,
                   coverage_json, started_at
            FROM runs WHERE {where} ORDER BY started_at DESC""",
        params,
    )
    grouped = {workload: [] for workload in _WORKLOADS}
    for run in runs:
        grouped[run["workload"]].append(run)

    profiles = [
        _profile_for(workload, grouped[workload], started_after)
        for workload in _WORKLOADS
    ]
    return {
        "formula_version": _FORMULA_VERSION,
        "window": {"hours": hours, "started_after": started_after},
        "profiles": profiles,
    }


def _profile_for(
    workload: str,
    runs: list[dict[str, Any]],
    started_after: str | None,
) -> dict[str, Any]:
    trace_ids = [run["trace_id"] for run in runs]
    statuses = {name: 0 for name in ("completed", "failed", "cancelled", "interrupted", "running")}
    integrity = {name: 0 for name in ("verified", "incomplete", "conflict", "legacy")}
    coverage_fields: dict[str, dict[str, int]] = {}
    coverage_complete = 0
    durations: list[int] = []
    for run in runs:
        statuses[run["status"]] = statuses.get(run["status"], 0) + 1
        integrity[run["integrity_status"]] = integrity.get(run["integrity_status"], 0) + 1
        if run["duration_ms"] is not None:
            durations.append(int(run["duration_ms"]))
        try:
            coverage = json.loads(run.get("coverage_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            coverage = {}
        is_complete = bool(coverage)
        for field, value in coverage.items():
            counts = coverage_fields.setdefault(
                str(field), {"known": 0, "partial": 0, "unknown": 0, "not_applicable": 0}
            )
            value_text = str(value)
            counts[value_text] = counts.get(value_text, 0) + 1
            if value_text not in {"known", "not_applicable"}:
                is_complete = False
        coverage_complete += int(is_complete)

    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "max_span_depth": 0}
    mechanisms = {
        "skill_activations": 0,
        "middleware_interventions": 0,
        "retries": 0,
        "hitl_events": 0,
    }
    if trace_ids:
        workload_where = "r.schema_version>=2 AND r.workload=?"
        workload_params: list[Any] = [workload]
        if started_after:
            workload_where += " AND r.started_at>=?"
            workload_params.append(started_after)
        usage_row = db.query_one(
            f"""SELECT COALESCE(SUM(usage_input),0) AS input_tokens,
                       COALESCE(SUM(usage_output),0) AS output_tokens,
                       COALESCE(SUM(usage_total),0) AS total_tokens,
                       COALESCE(MAX(depth),0) AS max_span_depth
                FROM nodes n JOIN runs r ON r.trace_id=n.trace_id
                WHERE {workload_where}""",
            tuple(workload_params),
        )
        if usage_row:
            usage = {key: int(usage_row[key] or 0) for key in usage}
        events = db.query_all(
            f"""SELECT e.type, e.payload_json FROM event_payloads e
                JOIN runs r ON r.trace_id=e.trace_id
                WHERE {workload_where} AND e.type IN
                ('skill_activation','middleware_intervention','hitl')""",
            tuple(workload_params),
        )
        for event in events:
            if event["type"] == "skill_activation":
                mechanisms["skill_activations"] += 1
            elif event["type"] == "hitl":
                mechanisms["hitl_events"] += 1
            else:
                mechanisms["middleware_interventions"] += 1
                try:
                    payload = json.loads(event["payload_json"])
                    if (payload.get("intervention") or {}).get("action") == "retry":
                        mechanisms["retries"] += 1
                except (json.JSONDecodeError, TypeError):
                    pass

    status_denominator = statuses["completed"] + statuses["failed"]
    sample_size = len(runs)
    advanced_missing = _advanced_analysis_missing(
        workload=workload,
        sample_size=sample_size,
        coverage_complete=coverage_complete,
        started_after=started_after,
    )
    return {
        "workload": workload,
        "sample_size": sample_size,
        "status_denominator": status_denominator,
        "statuses": statuses,
        "success_rate": (
            statuses["completed"] / status_denominator
            if status_denominator
            else None
        ),
        "integrity": {**integrity, "denominator": sample_size},
        "coverage": {
            "complete": coverage_complete,
            "denominator": sample_size,
            "fields": coverage_fields,
        },
        "duration_ms": {
            "p50": _percentile(durations, 0.5),
            "p90": _percentile(durations, 0.9),
            "p99": _percentile(durations, 0.99),
        },
        "usage": usage,
        "mechanisms": mechanisms,
        "drilldown_trace_ids": trace_ids[:5],
        "advanced_analysis": {
            "eligible": not advanced_missing,
            "missing_conditions": advanced_missing,
        },
    }


def _advanced_analysis_missing(
    *,
    workload: str,
    sample_size: int,
    coverage_complete: int,
    started_after: str | None,
) -> list[str]:
    if workload != "creation":
        return ["workload=creation"]
    missing: list[str] = []
    if sample_size < _ADVANCED_MIN_SAMPLE:
        missing.append(f"sample_size>={_ADVANCED_MIN_SAMPLE}")
    if not sample_size or coverage_complete / sample_size < 0.95:
        missing.append("coverage>=95%")
    time_sql = " AND r.started_at>=?" if started_after else ""
    time_params: tuple[Any, ...] = (started_after,) if started_after else ()
    sealed = db.query_one(
        f"""SELECT COUNT(*) AS count FROM evaluation_dossiers d
            JOIN runs r ON r.trace_id=d.trace_id
            WHERE d.seal_status='sealed' AND d.completeness_status='complete'
              AND r.schema_version>=2 AND r.workload='creation'{time_sql}""",
        time_params,
    )
    if not sealed or not sealed["count"]:
        missing.append("sealed_evaluation_sample")
    versioned = db.query_one(
        f"""SELECT COUNT(*) AS count FROM score_records s
            JOIN runs r ON s.target_type='trace' AND s.target_id=r.trace_id
            WHERE s.rubric_version NOT IN ('', 'unknown')
              AND r.schema_version>=2 AND r.workload='creation'{time_sql}""",
        time_params,
    )
    if not versioned or not versioned["count"]:
        missing.append("versioned_rubric")
    direct_time = " AND direct.started_at>=?" if started_after else ""
    revision_time = " AND producer.started_at>=?" if started_after else ""
    outcome_params: tuple[Any, ...] = (
        (started_after, started_after) if started_after else ()
    )
    outcomes = db.query_one(
        f"""SELECT COUNT(DISTINCT o.outcome_id) AS count
            FROM outcome_records o
            LEFT JOIN runs direct
              ON o.target_type='trace' AND o.target_id=direct.trace_id
            LEFT JOIN artifact_revisions revision
              ON o.target_type='artifact_revision'
             AND o.target_id=revision.artifact_revision_id
            LEFT JOIN runs producer ON producer.trace_id=revision.producer_trace_id
            WHERE (direct.schema_version>=2 AND direct.workload='creation'{direct_time})
               OR (producer.schema_version>=2 AND producer.workload='creation'{revision_time})""",
        outcome_params,
    )
    if not outcomes or not outcomes["count"]:
        missing.append("linked_outcome")
    experiments = db.query_one(
        f"""SELECT COUNT(*) AS count FROM experiment_runs_v2 experiment
            JOIN evaluation_dossiers d
              ON d.dossier_id=experiment.source_evaluation_dossier_id
            JOIN runs r ON r.trace_id=d.trace_id
            WHERE experiment.status='completed'
              AND experiment.formula_version NOT IN ('', 'unknown')
              AND d.seal_status='sealed' AND d.completeness_status='complete'
              AND r.schema_version>=2 AND r.workload='creation'{time_sql}""",
        time_params,
    )
    if not experiments or not experiments["count"]:
        missing.append("completed_experiment")
    return missing


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


__all__ = [
    "router", "get_lineage", "get_artifact_revision",
    "get_artifact_revision_content", "list_trace_artifact_revisions", "workload_profiles",
]
