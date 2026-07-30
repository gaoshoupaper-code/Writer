"""trace 摄入器：jsonl events → 投影 → 写入 SQLite 三表。

数据流：read_events → 推导 run summary → projector 投影 → 写 runs/nodes/event_payloads。
完全从 events 自洽推导，不依赖后端的 index.json（那是后端运行态）。
"""

from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import app.core.db as db
from app.ingestion import loader, projector
from app.core.models import TraceLogEvent, TraceNode, TraceRunSummary, TraceUsage
from contracts.trace import compute_trace_events_hash
from contracts.trace.payload import ContentAddressedPayloadStore, PayloadRejected
from app.core.settings import settings


def ingest_trace(trace_path: Path, workspace_id_hint: str | None = None) -> str | None:
    """摄入一个 trace jsonl：投影 + 入库。

    Args:
        trace_path: trace jsonl 文件绝对路径。
        workspace_id_hint: 可选的 workspace_id 提示（兜底扫描时已知，避免空值）。

    Returns:
        摄入的 trace_id；若文件无有效事件则返回 None。
    """
    events = loader.read_events(trace_path)
    if not events:
        return None
    return ingest_events(events, workspace_id_hint, trace_path)


def ingest_events(
    events: list[TraceLogEvent],
    workspace_id_hint: str | None = None,
    trace_path: Path | None = None,
    prior_events: list[TraceLogEvent] | None = None,
    run_status_hint: str | None = None,
    run_summary_hint: TraceRunSummary | None = None,
    payload_values: dict[str, Any] | None = None,
) -> str | None:
    """摄入已解析的事件列表：投影 + 入库（Phase 3 HTTP 拉取入口）。

    与 ingest_trace 的区别：不读文件，直接接收 events（从 executor HTTP 端点拉取后调用）。
    trace_path 可选——仅用于 run summary 的路径字段记录（debugging 用），解耦后可为 None。

    增量支持（D8）：prior_events 传入本地已入库的旧事件，与本次拉取的增量事件合并后
    全量投影。无 prior_events 时为首次摄入（全量）。nodes 必须全量重投影（projector
    需完整事件流配对），故内部仍 DELETE+INSERT。

    run_status_hint：executor run summary 的权威 status。事件流是历史日志，无法表达
    "此刻仍在运行"——只有 run_start 没有终结事件时，_derive_run_summary 默认判 failed，
    hint 用于纠正这种"运行中被误判失败"的场景（详情见 _derive_run_summary）。

    Returns:
        摄入的 trace_id；若无有效事件则返回 None。
    """
    # 合并旧事件（增量场景）：event_id 是幂等键，sequence 只用于连续性校验。
    if prior_events:
        seen = {e.event_id for e in events}
        merged = list(events) + [e for e in prior_events if e.event_id not in seen]
        all_events = sorted(merged, key=lambda e: e.sequence)
    else:
        all_events = events

    if not all_events:
        return None

    run, owner_user_id = _derive_run_summary(
        all_events, trace_path, workspace_id_hint, run_status_hint
    )

    if run_summary_hint is not None:
        run.schema_version = run_summary_hint.schema_version
        run.service = run_summary_hint.service
        run.workload = run_summary_hint.workload
        run.purpose = run_summary_hint.purpose
        run.coverage = run_summary_hint.coverage
        run.run_snapshot = run_summary_hint.run_snapshot
        run.links = run_summary_hint.links
        run.external_refs = run_summary_hint.external_refs
        run.manifest = run_summary_hint.manifest
        # 四维正交字段（DEC-008）：executor 权威摘要携带 phase/cancel_audit/revision，
        # 透传给本地 run（_write_run 会单调保护 revision）。
        run.trace_phase = run_summary_hint.trace_phase
        run.cancel_audit = run_summary_hint.cancel_audit
        run.lifecycle_revision = run_summary_hint.lifecycle_revision

    with db.transaction() as conn:
        # event_payloads 外键指向 runs；先建立 provisional run，最终完整性同事务回写。
        _write_run(conn, run, owner_user_id, 0)
        conflicts = _write_events(conn, run.trace_id, all_events)
        canonical_events = _load_canonical_events(conn, run.trace_id)
        run, owner_user_id = _derive_run_summary(
            canonical_events, trace_path, workspace_id_hint, run_status_hint
        )
        if run_summary_hint is not None:
            run.schema_version = run_summary_hint.schema_version
            run.service = run_summary_hint.service
            run.workload = run_summary_hint.workload
            run.purpose = run_summary_hint.purpose
            run.coverage = run_summary_hint.coverage
            run.run_snapshot = run_summary_hint.run_snapshot
            run.links = run_summary_hint.links
            run.external_refs = run_summary_hint.external_refs
            run.manifest = run_summary_hint.manifest
            # 四维正交字段（DEC-008）：receipt 计算可能纠正 phase/integrity，
            # 但 cancel_audit/revision 必须保留 executor 权威值。
            run.trace_phase = run_summary_hint.trace_phase
            run.cancel_audit = run_summary_hint.cancel_audit
            run.lifecycle_revision = run_summary_hint.lifecycle_revision
        payload_complete = _store_payload_metadata(
            conn, run.trace_id, canonical_events, payload_values or {}
        )
        _materialize_artifact_revisions(conn, run, canonical_events)
        receipt = _upsert_receipt(
            conn, run, canonical_events, conflicts, payload_complete
        )
        run.integrity_status = receipt["integrity_status"]
        # FR-008：receipt 计算后同步 trace_phase——verified=sealed，其余非 pending=degraded。
        # sealed 表示"已封存且结构可信"；degraded 表示"已封存但有缺口/冲突/降级"。
        if run.integrity_status == "verified":
            run.trace_phase = "sealed"
        elif run.integrity_status in ("incomplete", "conflict"):
            run.trace_phase = "degraded"
        # legacy / pending 保持原 phase（legacy 无 phase；pending 是运行中，receipt 不应到达）。
        _write_run(conn, run, owner_user_id, receipt["contiguous_seq"])
        conn.execute("DELETE FROM nodes WHERE trace_id = ?", (run.trace_id,))
        _write_nodes(conn, run.trace_id, run, canonical_events)

    from app.dossier.eligibility import assess_creation_trace

    assess_creation_trace(run.trace_id)
    return run.trace_id


def _derive_run_summary(
    events: list[TraceLogEvent],
    trace_path: Path | None,
    workspace_id_hint: str | None,
    run_status_hint: str | None = None,
) -> TraceRunSummary:
    """从 events 自洽推导 TraceRunSummary。

    status 推导优先级：
      1. 事件流终结事件（run_end/run_error/run_cancelled/run_awaiting）—— 终态以事件为准
      2. run_status_hint（executor run summary 权威 status）—— 仅在事件流未识别出
         明确状态（即落到默认 failed 分支）时覆盖，纠正"运行中无终结事件被误判失败"
      3. 默认 "failed"（异常终止，未正常收尾）
    """
    run_start = next((e for e in events if e.type == "run_start"), None)
    run_end = next((e for e in events if e.type == "run_end"), None)
    run_error = next((e for e in events if e.type == "run_error"), None)
    run_awaiting = next((e for e in events if e.type == "run_awaiting"), None)
    run_cancelled = next((e for e in events if e.type == "run_cancelled"), None)

    # run_start 的 input 携带 endpoint/thread_id/workspace_id/session_name
    start_input = (run_start.input if run_start and isinstance(run_start.input, dict) else {}) or {}

    trace_id = events[0].trace_id
    started_at = run_start.timestamp if run_start else events[0].timestamp
    ended_at: str | None = None
    status: str = "failed"   # 默认 failed：既无 run_end 也无 run_error = 异常终止（未正常收尾）
    duration_ms: int | None = None
    error: str | None = None

    if run_end:
        ended_at = run_end.timestamp
        status = run_end.status if run_end.status != "running" else "completed"
        duration_ms = run_end.duration_ms
    elif run_cancelled:
        ended_at = run_cancelled.timestamp
        status = "cancelled"
        duration_ms = run_cancelled.duration_ms
        error = run_cancelled.error
    elif run_error:
        ended_at = run_error.timestamp
        status = run_error.status
        duration_ms = run_error.duration_ms
        error = run_error.error
    elif run_awaiting:
        # awaiting_input 是中间态（非终态）：无 ended_at/duration_ms
        status = "awaiting_input"

    # executor run summary 是 status 权威源（事件流是历史日志，无法表达"此刻运行中"）。
    # 只有落到默认 failed 分支（事件流无终结事件）才用 hint 纠正——可能是真异常终止，
    # 也可能是运行中被扫描拉取；hint 区分两者。终态事件已识别时不覆盖（终态不回退）。
    if run_status_hint and status == "failed" and run_status_hint != "failed":
        status = run_status_hint

    # workspace_id：优先 run_start.input，其次 hint
    workspace_id = str(start_input.get("workspace_id") or workspace_id_hint or "unknown")

    # owner_user_id（Phase 3 D2/D20）：从 run_start.input 提取，缺省 'unknown'(T7)。
    owner_user_id = str(start_input.get("user_id") or "unknown")

    schema_version = max(event.schema_version for event in events)
    is_v2 = schema_version >= 2
    # FR-008/DEC-008：记录阶段 vs 封存阶段正交。运行中（无终态事件）的 trace 完整性
    # 是 pending（记录中/待校验），不是终态 incomplete——_upsert_receipt 会在终态后
    # 用 receipt 事实覆盖为 verified/incomplete。避免运行中被误报为数据损坏（EVD-005）。
    has_terminal = run_end is not None or run_error is not None or run_cancelled is not None
    if not is_v2:
        integrity_status = "legacy"
        trace_phase = None
    elif has_terminal:
        # 终态但 receipt 尚未计算——临时 incomplete，_upsert_receipt 会纠正。
        integrity_status = "incomplete"
        trace_phase = "sealed"
    else:
        integrity_status = "pending"
        trace_phase = "recording"

    return TraceRunSummary(
        trace_id=trace_id,
        workspace_id=workspace_id,
        thread_id=str(start_input.get("thread_id") or ""),
        session_name=str(start_input.get("session_name") or ""),
        # Phase 3 解耦：trace_path 可为 None（HTTP 拉取时无文件概念）。
        # workspace_path 仅 debugging 用，解耦后 evolution 看不到 executor 文件系统。
        workspace_path=str(trace_path.parent.parent) if trace_path else "",
        endpoint=str(start_input.get("endpoint") or ""),
        status=status,  # type: ignore[arg-type]
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        event_count=len(events),
        path=str(trace_path) if trace_path else "",
        error=error,
        schema_version=schema_version,
        service="executor" if is_v2 else None,
        workload="creation" if is_v2 else None,
        purpose=str(start_input.get("run_purpose") or "user_generation"),
        integrity_status=integrity_status,
        trace_phase=trace_phase,
        coverage={"payload": "unknown", "token": "unknown", "cost": "unknown"},
    ), owner_user_id


def _write_run(conn: Any, run: TraceRunSummary, owner_user_id: str = "unknown", ingested_seq: int = 0) -> None:
    cancel_audit_json = (
        json.dumps(run.cancel_audit.model_dump(mode="json"), ensure_ascii=False)
        if run.cancel_audit is not None
        else None
    )
    conn.execute(
        """INSERT INTO runs
           (trace_id, workspace_id, thread_id, session_name, endpoint, status,
            started_at, ended_at, duration_ms, event_count, error, ingested_at,
            owner_user_id, ingested_seq, schema_version, service, workload, run_purpose,
            integrity_status, coverage_json, run_snapshot_json, external_refs_json, links_json,
            trace_phase, cancel_audit, lifecycle_revision)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(trace_id) DO UPDATE SET
             workspace_id=excluded.workspace_id, thread_id=excluded.thread_id,
             session_name=excluded.session_name, endpoint=excluded.endpoint, status=excluded.status,
             started_at=excluded.started_at, ended_at=excluded.ended_at, duration_ms=excluded.duration_ms,
             event_count=excluded.event_count, error=excluded.error, ingested_at=excluded.ingested_at,
             owner_user_id=excluded.owner_user_id, ingested_seq=excluded.ingested_seq,
             schema_version=excluded.schema_version, service=excluded.service, workload=excluded.workload,
             run_purpose=excluded.run_purpose, integrity_status=excluded.integrity_status,
             coverage_json=excluded.coverage_json, run_snapshot_json=excluded.run_snapshot_json,
             external_refs_json=excluded.external_refs_json, links_json=excluded.links_json,
             trace_phase=excluded.trace_phase, cancel_audit=excluded.cancel_audit,
             lifecycle_revision=MAX(runs.lifecycle_revision, excluded.lifecycle_revision)""",
        (
            run.trace_id, run.workspace_id, run.thread_id, run.session_name, run.endpoint,
            run.status, run.started_at, run.ended_at, run.duration_ms, run.event_count,
            run.error, datetime.now(UTC).isoformat(),
            owner_user_id, ingested_seq, run.schema_version, run.service, run.workload,
            run.purpose, run.integrity_status, json.dumps(run.coverage),
            json.dumps(run.run_snapshot), json.dumps(run.external_refs),
            json.dumps([link.model_dump(mode="json") for link in run.links]),
            run.trace_phase, cancel_audit_json, run.lifecycle_revision,
        ),
    )


def _load_canonical_events(conn: Any, trace_id: str) -> list[TraceLogEvent]:
    """只返回已被唯一键规则接受的事实，冲突投递不得进入投影。"""
    rows = conn.execute(
        "SELECT payload_json FROM event_payloads WHERE trace_id=? ORDER BY sequence",
        (trace_id,),
    ).fetchall()
    return [TraceLogEvent.model_validate(json.loads(row["payload_json"])) for row in rows]


def _write_events(conn: Any, trace_id: str, events: list[TraceLogEvent]) -> bool:
    """至少一次投递：同内容幂等，不同内容显式进入冲突审计。"""
    conflicts = False
    existing_rows = conn.execute(
        "SELECT event_id, sequence, event_hash, payload_json FROM event_payloads WHERE trace_id=?",
        (trace_id,),
    ).fetchall()
    existing_by_id = {row["event_id"]: row for row in existing_rows if row["event_id"]}
    existing_by_seq = {row["sequence"]: row for row in existing_rows}
    for event in events:
        payload_json = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        event_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        same_id = existing_by_id.get(event.event_id)
        same_seq = existing_by_seq.get(event.sequence)
        collision = same_id or same_seq
        if collision is not None:
            existing_hash = collision["event_hash"]
            if not existing_hash:
                try:
                    existing = TraceLogEvent.model_validate(
                        json.loads(collision["payload_json"])
                    )
                    existing_json = json.dumps(
                        existing.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    existing_hash = hashlib.sha256(existing_json.encode("utf-8")).hexdigest()
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_hash = "legacy-unreadable"
            if existing_hash == event_hash:
                if collision["event_hash"] is None:
                    conn.execute(
                        "UPDATE event_payloads SET event_hash=? WHERE trace_id=? AND sequence=?",
                        (event_hash, trace_id, event.sequence),
                    )
                continue
            conflicts = True
            key = f"event_id:{event.event_id}" if same_id else f"sequence:{event.sequence}"
            conn.execute(
                """INSERT OR IGNORE INTO integrity_conflicts
                   (trace_id, conflict_key, existing_hash, received_hash, received_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (trace_id, key, existing_hash, event_hash, datetime.now(UTC).isoformat()),
            )
            continue
        conn.execute(
            """INSERT INTO event_payloads
               (trace_id, sequence, type, timestamp, payload_json, event_id, event_hash, payload_refs_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (trace_id, event.sequence, event.type, event.timestamp, payload_json, event.event_id,
             event_hash, json.dumps({key: ref.model_dump(mode="json") for key, ref in event.payload_refs.items()})),
        )
        existing_by_id[event.event_id] = {
            "event_hash": event_hash,
            "payload_json": payload_json,
        }
        existing_by_seq[event.sequence] = {
            "event_hash": event_hash,
            "payload_json": payload_json,
        }
    return conflicts


def _store_payload_metadata(
    conn: Any,
    trace_id: str,
    events: list[TraceLogEvent],
    payload_values: dict[str, Any],
) -> bool:
    """持久化 payload 元数据和引用，返回是否所有引用都有可读正文。"""
    complete = True
    store = ContentAddressedPayloadStore(settings.trace_payload_path)
    for event in events:
        for field_name, ref in event.payload_refs.items():
            if ref.payload_id not in payload_values:
                if not _stored_payload_matches(conn, store, ref.payload_id, ref.content_hash):
                    complete = False
            else:
                try:
                    stored_ref = store.put(payload_values[ref.payload_id], kind=ref.kind)
                    if stored_ref.content_hash != ref.content_hash:
                        complete = False
                        continue
                except (PayloadRejected, OSError, ValueError, TypeError):
                    complete = False
                    continue
            storage_path = str(settings.trace_payload_path / f"{ref.payload_id}.json")
            conn.execute(
                """INSERT INTO payload_objects
                   (payload_id, content_hash, kind, size_bytes, sensitivity, expires_at, storage_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(payload_id) DO UPDATE SET expires_at=excluded.expires_at""",
                (ref.payload_id, ref.content_hash, ref.kind, ref.size_bytes, ref.sensitivity,
                 ref.expires_at, storage_path, datetime.now(UTC).isoformat()),
            )
            conn.execute(
                """INSERT INTO trace_payload_links(trace_id, event_id, field_name, payload_id)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(trace_id, event_id, field_name) DO UPDATE SET payload_id=excluded.payload_id""",
                (trace_id, event.event_id, field_name, ref.payload_id),
            )
    return complete


def _stored_payload_matches(
    conn: Any,
    store: ContentAddressedPayloadStore,
    payload_id: str,
    expected_hash: str,
) -> bool:
    row = conn.execute(
        "SELECT content_hash, deleted_at FROM payload_objects WHERE payload_id=?", (payload_id,)
    ).fetchone()
    if row is None or row["deleted_at"] or row["content_hash"] != expected_hash:
        return False
    try:
        prepared = store.gate.prepare(store.get(payload_id))
    except (PayloadRejected, OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return prepared.content_hash == expected_hash


def _upsert_receipt(
    conn: Any,
    run: TraceRunSummary,
    events: list[TraceLogEvent],
    conflicts: bool,
    payload_complete: bool,
) -> dict[str, Any]:
    sequences = sorted({event.sequence for event in events if event.sequence > 0})
    contiguous = 0
    for sequence in sequences:
        if sequence != contiguous + 1:
            break
        contiguous = sequence
    max_seen = sequences[-1] if sequences else 0
    missing_ranges = _missing_ranges(sequences, contiguous, max_seen)
    terminal_ids = {event.event_id for event in events if event.type in {"run_end", "run_error", "run_cancelled"}}
    manifest = run.manifest
    manifest_ok = bool(
        manifest
        and manifest.final_sequence == contiguous
        and manifest.terminal_event_id in terminal_ids
        and manifest.events_hash == compute_trace_events_hash(events)
        and sorted(manifest.payload_ids)
        == sorted({ref.payload_id for event in events for ref in event.payload_refs.values()})
        and not manifest.capture_degraded
    )
    prior_conflict = conn.execute(
        "SELECT 1 FROM integrity_conflicts WHERE trace_id=? LIMIT 1", (run.trace_id,)
    ).fetchone()
    if run.schema_version < 2:
        integrity = "legacy"
    elif conflicts or prior_conflict is not None:
        integrity = "conflict"
    elif manifest_ok and not missing_ranges and payload_complete:
        integrity = "verified"
    else:
        integrity = "incomplete"
    prior = conn.execute("SELECT receipt_revision FROM trace_receipts WHERE trace_id=?", (run.trace_id,)).fetchone()
    revision = int(prior["receipt_revision"] if prior else 0) + 1
    conn.execute(
        """INSERT INTO trace_receipts
           (trace_id, contiguous_seq, max_seen_seq, missing_ranges_json, manifest_json,
            manifest_status, receipt_revision, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(trace_id) DO UPDATE SET contiguous_seq=excluded.contiguous_seq,
             max_seen_seq=excluded.max_seen_seq, missing_ranges_json=excluded.missing_ranges_json,
             manifest_json=excluded.manifest_json, manifest_status=excluded.manifest_status,
             receipt_revision=excluded.receipt_revision, updated_at=excluded.updated_at""",
        (run.trace_id, contiguous, max_seen, json.dumps(missing_ranges),
         manifest.model_dump_json() if manifest else None,
         "verified" if manifest_ok else ("missing" if manifest is None else "invalid"),
         revision, datetime.now(UTC).isoformat()),
    )
    return {"contiguous_seq": contiguous, "integrity_status": integrity}


def _materialize_artifact_revisions(
    conn: Any, run: TraceRunSummary, events: list[TraceLogEvent]
) -> None:
    store = ContentAddressedPayloadStore(settings.trace_payload_path)
    for event in events:
        if event.type != "artifact_revision" or not event.artifact_revision_id or not event.artifact:
            continue
        payload_ref = event.payload_refs.get("output")
        if payload_ref is None:
            continue
        if not _stored_payload_matches(
            conn, store, payload_ref.payload_id, payload_ref.content_hash
        ):
            continue
        logical_key = str(event.artifact.get("logical_key") or "")
        artifact_type = str(event.artifact.get("artifact_type") or "workspace_file")
        artifact_id = "artifact-" + hashlib.sha256(
            f"{run.workspace_id}:{artifact_type}:{logical_key}".encode("utf-8")
        ).hexdigest()[:32]
        conn.execute(
            """INSERT INTO artifacts(artifact_id, artifact_type, workspace_id, logical_key, created_at)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT(artifact_id) DO NOTHING""",
            (artifact_id, artifact_type, run.workspace_id, logical_key, datetime.now(UTC).isoformat()),
        )
        conn.execute(
            """INSERT INTO artifact_revisions
               (artifact_revision_id, artifact_id, parent_revision_id, payload_id, content_hash,
                producer_trace_id, producer_event_id, harness_version, provenance, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'trace_time', ?)
               ON CONFLICT(artifact_revision_id) DO NOTHING""",
            (event.artifact_revision_id, artifact_id, event.artifact.get("parent_revision_id"),
             payload_ref.payload_id, event.artifact.get("content_hash") or payload_ref.content_hash,
             run.trace_id, event.event_id,
             run.run_snapshot.get("harness_version"), event.timestamp),
        )
        conn.execute(
            """INSERT INTO lineage_edges(from_type, from_id, relation, to_type, to_id, created_at)
               VALUES ('trace', ?, 'produces', 'artifact_revision', ?, ?)
               ON CONFLICT(from_type, from_id, relation, to_type, to_id) DO NOTHING""",
            (run.trace_id, event.artifact_revision_id, event.timestamp),
        )


def _missing_ranges(sequences: list[int], contiguous: int, max_seen: int) -> list[list[int]]:
    if max_seen <= contiguous:
        return []
    seen = set(sequences)
    ranges: list[list[int]] = []
    start: int | None = None
    for sequence in range(contiguous + 1, max_seen + 1):
        if sequence not in seen and start is None:
            start = sequence
        elif sequence in seen and start is not None:
            ranges.append([start, sequence - 1])
            start = None
    if start is not None:
        ranges.append([start, max_seen])
    return ranges


def _write_nodes(conn: Any, trace_id: str, run: TraceRunSummary, events: list[TraceLogEvent]) -> None:
    """投影 events → nodes 树 → 写入 nodes 表。"""
    projection = projector.TraceProjector().project(run, events)
    rows = [_node_row(trace_id, node) for node in projection.nodes]
    if rows:
        conn.executemany(
            """INSERT INTO nodes
               (trace_id, node_id, parent_node_id, kind, label, status,
                agent_name, agent_role, depth, started_at, ended_at, duration_ms,
                model_name, tool_name, skill_name,
                usage_input, usage_output, usage_total,
                chain_summary, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def _node_row(trace_id: str, node: TraceNode) -> tuple[Any, ...]:
    usage = node.usage or TraceUsage()
    return (
        trace_id, node.node_id, node.parent_node_id, node.kind, node.label, node.status,
        node.agent_name, node.agent_role, node.depth,
        node.started_at, node.ended_at, node.duration_ms,
        node.model_name, node.tool_name, node.skill_name,
        usage.input_tokens, usage.output_tokens, usage.total_tokens,
        node.chain_summary, node.error,
    )
