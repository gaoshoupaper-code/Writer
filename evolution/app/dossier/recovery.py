"""仅依据源 Trace 受治理 Payload 恢复最终 ArtifactRevision 头。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import app.core.db as db
from app.core.models import TraceLogEvent
from app.core.settings import settings
from app.dossier.tool_results import is_successful_tool_end
from app.trace.facts import add_lineage
from app.trace.recorder import EvolutionTraceRecorder
from app.trace_payloads import hydrate_event
from contracts.trace import TraceSpanLink
from contracts.trace.payload import ContentAddressedPayloadStore


_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
_VERSIONED_ARTIFACT_PATHS = frozenset({
    "/demand.md", "/outline.md", "/storyline.md", "/worldview.md", "/novel.md",
})
_VERSIONED_ARTIFACT_PREFIXES = (
    "/character/", "/storyline/", "/detail/", "/chapter/", "/review/",
)
_LINE_PREFIX = re.compile(r"^\s*(\d+)(?:\.(\d+))?\t(.*)$")


def _is_approved_cancelled_user_stop(run_row: dict[str, Any]) -> bool:
    """判定 run_row 是否为"已人工确认的用户主动停止 trace"（与 eligibility 同源条件）。

    四者同时成立：status='cancelled' + cancel_audit.reason='user_stop' +
    evidence_override_approved=1 + revoked_at 为 NULL（未撤回）。recovery 在
    allow_cancelled_approved=True 时复用本判定。
    """
    if str(run_row.get("status") or "") != "cancelled":
        return False
    if not run_row.get("evidence_override_approved"):
        return False
    if run_row.get("evidence_override_revoked_at"):
        return False
    cancel_audit_raw = run_row.get("cancel_audit")
    if not cancel_audit_raw:
        return False
    try:
        cancel_audit = (
            json.loads(cancel_audit_raw)
            if isinstance(cancel_audit_raw, str)
            else cancel_audit_raw
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(cancel_audit, dict) and cancel_audit.get("reason") == "user_stop"
_MAX_LINEARIZATIONS = 200_000


class TracePayloadRecoveryError(RuntimeError):
    """源 Trace 事实不足以确定性重建一个或多个最终头。"""


@dataclass(frozen=True)
class ToolInterval:
    event_id: str
    start_sequence: int
    end_sequence: int
    tool_call_id: str
    tool_name: str
    agent_name: str
    path: str
    args: dict[str, Any]
    payload_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReadObservation:
    start_sequence: int
    end_sequence: int
    path: str
    lines: dict[int, str]
    event_id: str
    payload_ids: tuple[str, ...]


@dataclass(frozen=True)
class RecoveredArtifactHead:
    path: str
    content: str
    content_hash: str
    final_operation: ToolInterval
    support_event_ids: tuple[str, ...]
    support_payload_ids: tuple[str, ...]


def recover_trace_artifacts(
    source_trace_id: str,
    *,
    expected_head_count: int | None = None,
    recorder: EvolutionTraceRecorder | None = None,
    allow_cancelled_approved: bool = False,
) -> dict[str, Any]:
    """恢复源 Trace 的唯一最终头并写入新的 sealed recovery Trace。

    allow_cancelled_approved（REQ-20260802-211032，FR-002）：默认 False 保持旧行为
    （仅 completed trace 可恢复）。为 True 时放宽到接受"已人工确认的用户主动停止
    trace"（status='cancelled' + cancel_audit.reason='user_stop' +
    evidence_override_approved=1）。hash 校验与重建算法完全不变——只放宽 source
    终态准入；停止前已成功 write 的半成品照常走确定性恢复。
    """
    source_run = db.query_one("SELECT * FROM runs WHERE trace_id=?", (source_trace_id,))
    if source_run is None:
        raise TracePayloadRecoveryError(f"source trace not found: {source_trace_id}")
    if source_run.get("service") != "executor" or source_run.get("workload") != "creation":
        raise TracePayloadRecoveryError("source trace is not an executor creation trace")
    status = str(source_run.get("status") or "")
    integrity = str(source_run.get("integrity_status") or "")
    if integrity != "verified":
        raise TracePayloadRecoveryError("source trace is not transport-verified")
    # completed 走标准路径；cancelled 仅在显式授权且满足"已确认 user_stop"三条件时放行。
    if status != "completed":
        if not (allow_cancelled_approved and _is_approved_cancelled_user_stop(source_run)):
            raise TracePayloadRecoveryError(
                "source trace is not completed and transport-verified"
            )

    existing = _existing_recovery(source_trace_id)
    if existing is not None:
        if expected_head_count is not None and existing["recovered_head_count"] != expected_head_count:
            raise TracePayloadRecoveryError(
                f"existing recovery has {existing['recovered_head_count']} heads, "
                f"expected {expected_head_count}"
            )
        return existing

    events = _load_hydrated_events(source_trace_id)
    operations, observations = _collect_intervals(events)
    heads = reconstruct_artifact_heads(operations, observations)
    if expected_head_count is not None and len(heads) != expected_head_count:
        raise TracePayloadRecoveryError(
            f"recovered {len(heads)} unique heads, expected {expected_head_count}"
        )

    demand_snapshot, demand_support = _recover_contract_snapshot(events)
    active_recorder = recorder or EvolutionTraceRecorder()
    handle = active_recorder.create_run(
        session_id=f"recovery:{source_trace_id}",
        run_purpose="trace_payload_recovery",
        endpoint="dossier.trace_payload_recovery",
        workload="evidence_compile",
        links=[TraceSpanLink(target_trace_id=source_trace_id, relation="derived_from")],
        external_refs={"source_trace_id": source_trace_id},
    )
    recovery_trace_id = handle.trace_id
    active_recorder.append_event(
        recovery_trace_id,
        {
            "type": "run_meta",
            "status": "running",
            "source": "system",
            "input": {
                "recovery": {
                    "provenance": "trace_payload_recovery",
                    "source_trace_id": source_trace_id,
                    "head_count": len(heads),
                    "source_event_count": int(source_run.get("event_count") or 0),
                },
                "contract_snapshot": {
                    "task_type": source_run.get("endpoint") or "creation",
                    "run_purpose": source_run.get("run_purpose"),
                    "endpoint": source_run.get("endpoint"),
                    "thread_id": source_run.get("thread_id"),
                    "workspace_id": source_run.get("workspace_id"),
                    "session_name": source_run.get("session_name"),
                    "demand_md": demand_snapshot,
                    "demand_available": demand_snapshot is not None,
                    "missing": [] if demand_snapshot is not None else ["demand.md"],
                    "support_event_ids": list(demand_support[0]),
                    "support_payload_ids": list(demand_support[1]),
                },
            },
        },
    )

    pending_rows: list[dict[str, Any]] = []
    store = ContentAddressedPayloadStore(settings.trace_payload_path)
    try:
        for head in heads:
            payload_ref = store.put({"content": head.content})
            _store_payload_metadata(payload_ref)
            revision_id = "artifact-rev-recovery-" + hashlib.sha256(
                f"{source_trace_id}:{head.path}:{head.content_hash}".encode("utf-8")
            ).hexdigest()[:32]
            event = active_recorder.append_event(
                recovery_trace_id,
                {
                    "type": "artifact_revision",
                    "status": "completed",
                    "source": "runtime",
                    "agent_name": head.final_operation.agent_name,
                    "tool_name": head.final_operation.tool_name,
                    "tool_call_id": head.final_operation.tool_call_id,
                    "artifact_revision_id": revision_id,
                    "artifact": {
                        "logical_key": head.path,
                        "artifact_type": "workspace_file",
                        "content_hash": head.content_hash,
                        "provenance": "trace_payload_recovery",
                        "source_trace_id": source_trace_id,
                        "support_event_ids": list(head.support_event_ids),
                        "support_payload_ids": list(head.support_payload_ids),
                    },
                    "_precomputed_payload_refs": {"output": payload_ref},
                },
            )
            pending_rows.append({
                "head": head,
                "payload_ref": payload_ref,
                "revision_id": revision_id,
                "event_id": event.event_id,
            })
        active_recorder.complete_run(recovery_trace_id)
    except BaseException as exc:
        active_recorder.fail_run(recovery_trace_id, exc)
        raise

    _materialize_recovery_rows(
        source_run=source_run,
        source_trace_id=source_trace_id,
        recovery_trace_id=recovery_trace_id,
        rows=pending_rows,
    )
    return {
        "source_trace_id": source_trace_id,
        "recovery_trace_id": recovery_trace_id,
        "recovered_head_count": len(heads),
        "heads": [
            {
                "path": head.path,
                "content_hash": head.content_hash,
                "support_event_count": len(head.support_event_ids),
                "support_payload_count": len(head.support_payload_ids),
            }
            for head in heads
        ],
    }


def reconstruct_artifact_heads(
    operations: list[ToolInterval], observations: list[ReadObservation]
) -> list[RecoveredArtifactHead]:
    by_path: dict[str, list[ToolInterval]] = {}
    for operation in operations:
        if operation.tool_name in _WRITE_TOOLS and _is_versioned_artifact_path(operation.path):
            by_path.setdefault(operation.path, []).append(operation)
    observation_by_path: dict[str, list[ReadObservation]] = {}
    for observation in observations:
        if observation.path in by_path:
            observation_by_path.setdefault(observation.path, []).append(observation)

    heads = [
        _reconstruct_path(path, path_operations, observation_by_path.get(path, []))
        for path, path_operations in sorted(by_path.items())
    ]
    return heads


def _reconstruct_path(
    path: str, operations: list[ToolInterval], observations: list[ReadObservation]
) -> RecoveredArtifactHead:
    predecessors = {
        operation.event_id: {
            candidate.event_id
            for candidate in operations
            if candidate.event_id != operation.event_id
            and candidate.end_sequence < operation.start_sequence
        }
        for operation in operations
    }
    by_id = {operation.event_id: operation for operation in operations}
    final_candidates: dict[str, tuple[str, tuple[ToolInterval, ...]]] = {}
    explored = 0

    def visit(
        ordered: tuple[ToolInterval, ...],
        remaining: frozenset[str],
        content: str | None,
        states: tuple[str | None, ...],
    ) -> None:
        nonlocal explored
        explored += 1
        if explored > _MAX_LINEARIZATIONS:
            raise TracePayloadRecoveryError(
                f"linearization limit exceeded for {path}: {_MAX_LINEARIZATIONS}"
            )
        if not remaining:
            if content is None:
                return
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            final_candidates.setdefault(digest, (content, ordered))
            return
        completed = {operation.event_id for operation in ordered}
        ready = sorted(
            (by_id[event_id] for event_id in remaining if predecessors[event_id] <= completed),
            key=lambda item: (item.end_sequence, item.start_sequence, item.event_id),
        )
        for operation in ready:
            next_content = _apply_operation(content, operation)
            if next_content is _INVALID:
                continue
            visit(
                (*ordered, operation),
                remaining - {operation.event_id},
                next_content,
                (*states, next_content),
            )

    visit((), frozenset(by_id), None, (None,))
    if len(final_candidates) > 1 and observations:
        final_candidates = {
            digest: candidate
            for digest, candidate in final_candidates.items()
            if _observations_fit(
                candidate[1],
                _states_for_order(candidate[1]),
                observations,
            )
        }
    if not final_candidates:
        observed_candidate = _recover_from_read_windows(operations, observations)
        if observed_candidate is not None:
            observed_hash = hashlib.sha256(observed_candidate[0].encode("utf-8")).hexdigest()
            final_candidates[observed_hash] = observed_candidate
    if len(final_candidates) != 1:
        hashes = sorted(final_candidates)
        raise TracePayloadRecoveryError(
            f"{path} has {len(hashes)} deterministic final hashes after payload replay"
        )
    content_hash, (content, ordered) = next(iter(final_candidates.items()))
    support_events = [operation.event_id for operation in ordered]
    support_payloads = [payload_id for operation in ordered for payload_id in operation.payload_ids]
    for observation in observations:
        support_events.append(observation.event_id)
        support_payloads.extend(observation.payload_ids)
    return RecoveredArtifactHead(
        path=path,
        content=content,
        content_hash=content_hash,
        final_operation=ordered[-1],
        support_event_ids=tuple(dict.fromkeys(support_events)),
        support_payload_ids=tuple(dict.fromkeys(support_payloads)),
    )


_INVALID = object()


def _apply_operation(content: str | None, operation: ToolInterval) -> str | object:
    if operation.tool_name == "write_file":
        value = operation.args.get("content")
        return value if content is None and isinstance(value, str) else _INVALID
    if content is None:
        return _INVALID
    old_string = operation.args.get("old_string")
    new_string = operation.args.get("new_string")
    replace_all = bool(operation.args.get("replace_all", False))
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return _INVALID
    old_string = old_string.replace("\r\n", "\n").replace("\r", "\n")
    new_string = new_string.replace("\r\n", "\n").replace("\r", "\n")
    if old_string == new_string:
        return content
    occurrences = content.count(old_string)
    if occurrences == 0 or (occurrences > 1 and not replace_all):
        return _INVALID
    return content.replace(old_string, new_string) if replace_all else content.replace(
        old_string, new_string, 1
    )


def _states_for_order(ordered: tuple[ToolInterval, ...]) -> tuple[str | None, ...]:
    states: list[str | None] = [None]
    content: str | None = None
    for operation in ordered:
        applied = _apply_operation(content, operation)
        if applied is _INVALID:
            return tuple(states)
        content = applied
        states.append(content)
    return tuple(states)


def _recover_from_read_windows(
    operations: list[ToolInterval], observations: list[ReadObservation]
) -> tuple[str, tuple[ToolInterval, ...]] | None:
    """用同 Trace 的前缀读快照、后续 edit 和最终窗口拼出完整正文。"""
    if not operations or not observations:
        return None
    final_end = max(operation.end_sequence for operation in operations)
    trailing_newline = False
    first_write = min(operations, key=lambda item: item.end_sequence)
    if first_write.tool_name == "write_file":
        initial_content = first_write.args.get("content")
        trailing_newline = isinstance(initial_content, str) and initial_content.endswith("\n")

    candidates: dict[str, tuple[str, tuple[ToolInterval, ...]]] = {}
    prefixes = [
        observation for observation in observations
        if observation.lines and min(observation.lines) == 1
    ]
    suffixes = [
        observation for observation in observations
        if observation.start_sequence > final_end
    ]
    for prefix in prefixes:
        prefix_content = "\n".join(prefix.lines[number] for number in sorted(prefix.lines))
        remaining = [
            operation for operation in operations
            if operation.start_sequence > prefix.end_sequence
        ]
        partial_orders = _linearized_contents(prefix_content, remaining)
        for partial_content, ordered_remaining in partial_orders:
            partial_lines = {
                index: value for index, value in enumerate(partial_content.splitlines(), start=1)
            }
            merged = dict(partial_lines)
            supportable = True
            for suffix in suffixes:
                overlap = set(merged) & set(suffix.lines)
                if any(merged[number] != suffix.lines[number] for number in overlap):
                    continue
                merged.update(suffix.lines)
            if not merged or min(merged) != 1 or set(merged) != set(range(1, max(merged) + 1)):
                supportable = False
            if not supportable:
                continue
            content = "\n".join(merged[number] for number in range(1, max(merged) + 1))
            if trailing_newline:
                content += "\n"
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            ordered_prefix = tuple(
                operation
                for operation in sorted(operations, key=lambda item: item.end_sequence)
                if operation.end_sequence <= prefix.start_sequence
            )
            candidates[digest] = (content, (*ordered_prefix, *ordered_remaining))
    if len(candidates) != 1:
        return None
    return next(iter(candidates.values()))


def _linearized_contents(
    initial_content: str, operations: list[ToolInterval]
) -> list[tuple[str, tuple[ToolInterval, ...]]]:
    predecessors = {
        operation.event_id: {
            candidate.event_id
            for candidate in operations
            if candidate.event_id != operation.event_id
            and candidate.end_sequence < operation.start_sequence
        }
        for operation in operations
    }
    by_id = {operation.event_id: operation for operation in operations}
    results: dict[str, tuple[str, tuple[ToolInterval, ...]]] = {}

    def visit(content: str, ordered: tuple[ToolInterval, ...], remaining: frozenset[str]) -> None:
        if not remaining:
            results[hashlib.sha256(content.encode("utf-8")).hexdigest()] = (content, ordered)
            return
        completed = {operation.event_id for operation in ordered}
        for operation in sorted(
            (by_id[event_id] for event_id in remaining if predecessors[event_id] <= completed),
            key=lambda item: (item.end_sequence, item.start_sequence, item.event_id),
        ):
            applied = _apply_operation(content, operation)
            if applied is _INVALID or not isinstance(applied, str):
                continue
            visit(applied, (*ordered, operation), remaining - {operation.event_id})

    visit(initial_content, (), frozenset(by_id))
    return list(results.values())


def _observations_fit(
    ordered: tuple[ToolInterval, ...],
    states: tuple[str | None, ...],
    observations: list[ReadObservation],
) -> bool:
    for observation in observations:
        minimum = 0
        maximum = len(ordered)
        for index, operation in enumerate(ordered):
            if operation.end_sequence < observation.start_sequence:
                minimum = max(minimum, index + 1)
            if operation.start_sequence > observation.end_sequence:
                maximum = min(maximum, index)
        if minimum > maximum:
            return False
        if not any(
            state is not None and _matches_observation(state, observation.lines)
            for state in states[minimum:maximum + 1]
        ):
            return False
    return True


def _matches_observation(content: str, observed_lines: dict[int, str]) -> bool:
    lines = content.splitlines()
    return all(0 < number <= len(lines) and lines[number - 1] == value for number, value in observed_lines.items())


def _collect_intervals(
    events: list[TraceLogEvent],
) -> tuple[list[ToolInterval], list[ReadObservation]]:
    starts = {
        event.tool_call_id: event
        for event in events
        if event.type == "tool_start" and event.tool_call_id
    }
    operations: list[ToolInterval] = []
    observations: list[ReadObservation] = []
    for event in events:
        if not is_successful_tool_end(event) or not event.tool_call_id:
            continue
        start = starts.get(event.tool_call_id)
        if start is None:
            continue
        args = event.tool_args if isinstance(event.tool_args, dict) else {}
        path = _normalize_path(args.get("file_path") or args.get("path"))
        payload_ids = tuple(
            dict.fromkeys(
                ref.payload_id
                for source_event in (start, event)
                for ref in source_event.payload_refs.values()
            )
        )
        interval = ToolInterval(
            event_id=event.event_id,
            start_sequence=start.sequence,
            end_sequence=event.sequence,
            tool_call_id=event.tool_call_id,
            tool_name=str(event.tool_name or ""),
            agent_name=str(event.agent_name or "unknown"),
            path=path,
            args=args,
            payload_ids=payload_ids,
        )
        if interval.tool_name in _WRITE_TOOLS:
            operations.append(interval)
        elif interval.tool_name in {"read_file", "read"}:
            lines = _parse_read_lines(event.tool_output)
            if lines:
                observations.append(ReadObservation(
                    start_sequence=start.sequence,
                    end_sequence=event.sequence,
                    path=path,
                    lines=lines,
                    event_id=event.event_id,
                    payload_ids=payload_ids,
                ))
    return operations, observations


def _parse_read_lines(tool_output: Any) -> dict[int, str]:
    content = tool_output.get("content") if isinstance(tool_output, dict) else tool_output
    if not isinstance(content, str):
        return {}
    parsed: dict[int, str] = {}
    continuations: dict[int, list[tuple[int, str]]] = {}
    for line in content.splitlines():
        match = _LINE_PREFIX.match(line)
        if match is None:
            return {}
        number = int(match.group(1))
        continuation = match.group(2)
        if continuation is None:
            parsed[number] = match.group(3)
        else:
            continuations.setdefault(number, []).append((int(continuation), match.group(3)))
    for number, chunks in continuations.items():
        if number not in parsed:
            return {}
        parsed[number] += "".join(value for _, value in sorted(chunks))
    return parsed


def _load_hydrated_events(trace_id: str) -> list[TraceLogEvent]:
    events: list[TraceLogEvent] = []
    for row in db.query_all(
        "SELECT payload_json FROM event_payloads WHERE trace_id=? ORDER BY sequence",
        (trace_id,),
    ):
        event = TraceLogEvent.model_validate(json.loads(row["payload_json"]))
        hydrated = hydrate_event(event)
        missing = set(event.payload_refs) - {
            field for field in event.payload_refs if getattr(hydrated, field, None) is not None
        }
        if missing:
            raise TracePayloadRecoveryError(
                f"event {event.event_id} has unreadable governed payloads: {sorted(missing)}"
            )
        events.append(hydrated)
    return events


def _recover_contract_snapshot(
    events: list[TraceLogEvent],
) -> tuple[str | None, tuple[tuple[str, ...], tuple[str, ...]]]:
    for event in events:
        if event.type == "run_meta" and isinstance(event.input, dict):
            snapshot = event.input.get("contract_snapshot")
            if isinstance(snapshot, dict) and isinstance(snapshot.get("demand_md"), str):
                return snapshot["demand_md"], ((event.event_id,), tuple(
                    ref.payload_id for ref in event.payload_refs.values()
                ))
    for event in events:
        if event.type != "tool_end" or event.tool_name not in {"read_file", "read"}:
            continue
        args = event.tool_args if isinstance(event.tool_args, dict) else {}
        if _normalize_path(args.get("file_path") or args.get("path")) != "/demand.md":
            continue
        lines = _parse_read_lines(event.tool_output)
        if lines and min(lines) == 1:
            demand = "\n".join(lines[number] for number in sorted(lines))
            return demand, ((event.event_id,), tuple(
                ref.payload_id for ref in event.payload_refs.values()
            ))
    return None, ((), ())


def _store_payload_metadata(ref: Any) -> None:
    db.execute(
        """INSERT INTO payload_objects
           (payload_id, content_hash, kind, size_bytes, sensitivity, expires_at,
            storage_path, sealed, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
           ON CONFLICT(payload_id) DO UPDATE SET sealed=1""",
        (
            ref.payload_id, ref.content_hash, ref.kind, ref.size_bytes, ref.sensitivity,
            ref.expires_at, str(settings.trace_payload_path / f"{ref.payload_id}.json"),
            datetime.now(UTC).isoformat(),
        ),
    )


def _materialize_recovery_rows(
    *, source_run: dict[str, Any], source_trace_id: str,
    recovery_trace_id: str, rows: list[dict[str, Any]],
) -> None:
    now = datetime.now(UTC).isoformat()
    with db.transaction() as conn:
        for row in rows:
            head: RecoveredArtifactHead = row["head"]
            artifact_id = "artifact-" + hashlib.sha256(
                f"{source_run['workspace_id']}:workspace_file:{head.path}".encode("utf-8")
            ).hexdigest()[:32]
            conn.execute(
                """INSERT INTO artifacts
                   (artifact_id, artifact_type, workspace_id, logical_key, created_at)
                   VALUES (?, 'workspace_file', ?, ?, ?)
                   ON CONFLICT(artifact_id) DO NOTHING""",
                (artifact_id, source_run["workspace_id"], head.path, now),
            )
            conn.execute(
                """INSERT INTO artifact_revisions
                   (artifact_revision_id, artifact_id, parent_revision_id, payload_id,
                    content_hash, producer_trace_id, producer_event_id, harness_version,
                    provenance, source_trace_id, support_event_ids_json,
                    support_payload_ids_json, created_at)
                   VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 'trace_payload_recovery', ?, ?, ?, ?)""",
                (
                    row["revision_id"], artifact_id, row["payload_ref"].payload_id,
                    head.content_hash, recovery_trace_id, row["event_id"],
                    json.loads(source_run.get("run_snapshot_json") or "{}").get("harness_version"),
                    source_trace_id, json.dumps(head.support_event_ids),
                    json.dumps(head.support_payload_ids), now,
                ),
            )
    add_lineage("trace", recovery_trace_id, "recovers", "trace", source_trace_id)
    for row in rows:
        head: RecoveredArtifactHead = row["head"]
        revision_id = row["revision_id"]
        add_lineage("trace", recovery_trace_id, "produces", "artifact_revision", revision_id)
        add_lineage("artifact_revision", revision_id, "recovers", "trace", source_trace_id)
        for event_id in head.support_event_ids:
            add_lineage("trace_event", event_id, "supports", "artifact_revision", revision_id)
        for payload_id in head.support_payload_ids:
            add_lineage("payload", payload_id, "supports", "artifact_revision", revision_id)


def _existing_recovery(source_trace_id: str) -> dict[str, Any] | None:
    rows = db.query_all(
        """SELECT producer_trace_id, artifact_revision_id, content_hash
           FROM artifact_revisions
           WHERE source_trace_id=? AND provenance='trace_payload_recovery'
           ORDER BY artifact_revision_id""",
        (source_trace_id,),
    )
    if not rows:
        return None
    trace_ids = {row["producer_trace_id"] for row in rows}
    if len(trace_ids) != 1:
        raise TracePayloadRecoveryError("source trace has conflicting recovery producers")
    return {
        "source_trace_id": source_trace_id,
        "recovery_trace_id": next(iter(trace_ids)),
        "recovered_head_count": len(rows),
        "heads": [
            {"artifact_revision_id": row["artifact_revision_id"], "content_hash": row["content_hash"]}
            for row in rows
        ],
        "idempotent_replay": True,
    }


def _normalize_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    path = "/" + value.replace("\\", "/").lstrip("/")
    return path.removeprefix("/workspace") or "/"


def _is_versioned_artifact_path(path: str) -> bool:
    return path in _VERSIONED_ARTIFACT_PATHS or path.startswith(_VERSIONED_ARTIFACT_PREFIXES)


__all__ = [
    "ReadObservation", "RecoveredArtifactHead", "ToolInterval",
    "TracePayloadRecoveryError", "recover_trace_artifacts", "reconstruct_artifact_heads",
]
