"""不可变 Harness candidate 的装配、证据、质量与身份门禁。"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core import db
from app.core.settings import settings


def probe_candidate(commit_hash: str) -> dict[str, Any]:
    """让拟发布 executor 从 candidate 干净 checkout 做真实最小装配。"""
    response = httpx.post(
        f"{settings.executor_url.rstrip('/')}/internal/harness/probe",
        json={"source_commit": commit_hash},
        timeout=120.0,
    )
    response.raise_for_status()
    result = response.json()
    if (
        result.get("harness_commit") != commit_hash
        or not result.get("assembled")
        or not result.get("artifact_snapshot_middleware")
    ):
        raise ValueError(f"candidate clean-checkout probe failed: {result}")
    return result


def validate_candidate_snapshot(
    candidate: dict[str, Any], probe: dict[str, Any]
) -> dict[str, Any]:
    """要求 snapshot 的源码、executor、依赖、证据与质量全量一致。"""
    version = candidate["version"]
    commit_hash = candidate["commit_hash"]
    row = db.query_one(
        """SELECT test.trace_id, run.status AS run_status,
                  run.integrity_status, run.evidence_status, run.run_snapshot_json
           FROM manual_tests test
           JOIN runs run ON run.trace_id=test.trace_id
           WHERE test.version_type='snapshot' AND test.version_id=? AND test.status='done'
             AND run.status='completed' AND run.integrity_status='verified'
             AND run.evidence_status='complete'
           ORDER BY test.created_at DESC LIMIT 1""",
        (version,),
    )
    if row is None:
        raise ValueError(
            f"candidate v{version} 尚无 transport/evidence 均完整的 snapshot Trace"
        )
    snapshot_identity = json.loads(row.get("run_snapshot_json") or "{}")
    probe_identity = probe.get("runtime_identity") or {}
    frozen_probe_identity = candidate.get("probe_identity") or {}
    gaps: list[str] = []
    if snapshot_identity.get("harness_commit") != commit_hash:
        gaps.append("harness_commit mismatch")
    if snapshot_identity.get("harness_dirty"):
        gaps.append("snapshot harness checkout is dirty")
    if not snapshot_identity.get("artifact_snapshot_middleware"):
        gaps.append("ArtifactSnapshotMiddleware missing")
    if not snapshot_identity.get("platform_artifact_capture"):
        gaps.append("platform artifact capture disabled")
    if snapshot_identity.get("identity_digest") != probe_identity.get("identity_digest"):
        gaps.append("runtime identity mismatch")
    if frozen_probe_identity.get("identity_digest") != probe_identity.get("identity_digest"):
        gaps.append("executor identity changed since candidate freeze")
    if gaps:
        raise ValueError("; ".join(gaps))

    evidence = db.query_one(
        """SELECT pack_id FROM evidence_dossiers
           WHERE trace_id=? AND status='ready' ORDER BY version DESC LIMIT 1""",
        (row["trace_id"],),
    )
    evaluation = db.query_one(
        """SELECT dossier_id FROM evaluation_dossiers
           WHERE trace_id=? AND seal_status='sealed' AND completeness_status='complete'
           ORDER BY created_at DESC LIMIT 1""",
        (row["trace_id"],),
    )
    if evidence is None:
        raise ValueError("candidate snapshot 缺少 ready 证据卷宗")
    if evaluation is None:
        raise ValueError("candidate snapshot 缺少 sealed complete 评估卷宗")
    return {
        "snapshot_trace_id": row["trace_id"],
        "evidence_dossier_id": evidence["pack_id"],
        "evaluation_dossier_id": evaluation["dossier_id"],
        "runtime_identity": snapshot_identity,
    }


__all__ = ["probe_candidate", "validate_candidate_snapshot"]
