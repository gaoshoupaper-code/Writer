"""Evolution 侧受控 Payload 读取与生命周期操作。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.core import db
from app.core.models import TraceLogEvent
from app.core.settings import settings
from contracts.trace.payload import ContentAddressedPayloadStore

logger = logging.getLogger("evolution.trace_payloads")
_LIFECYCLE_INTERVAL_SECONDS = 60 * 60
_lifecycle_task: asyncio.Task[None] | None = None


def hydrate_event(event: TraceLogEvent) -> TraceLogEvent:
    """仅在授权后的详情、卷宗和评估内部路径回填正文。"""
    if not event.payload_refs:
        return event
    values = event.model_dump()
    store = ContentAddressedPayloadStore(settings.trace_payload_path)
    for field_name, ref in event.payload_refs.items():
        row = db.query_one(
            "SELECT expires_at, deleted_at FROM payload_objects WHERE payload_id=?",
            (ref.payload_id,),
        )
        if row is None or row.get("deleted_at") or _expired(row.get("expires_at")):
            continue
        try:
            values[field_name] = store.get(ref.payload_id)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return TraceLogEvent.model_validate(values)


def read_payload(payload_id: str) -> Any | None:
    row = db.query_one(
        "SELECT expires_at, deleted_at FROM payload_objects WHERE payload_id=?", (payload_id,)
    )
    if row is None or row.get("deleted_at") or _expired(row.get("expires_at")):
        return None
    try:
        return ContentAddressedPayloadStore(settings.trace_payload_path).get(payload_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def purge_expired_payloads() -> int:
    rows = db.query_all(
        """SELECT payload_id, expires_at FROM payload_objects
           WHERE expires_at IS NOT NULL AND sealed=0 AND deleted_at IS NULL"""
    )
    expired = [row["payload_id"] for row in rows if _expired(row.get("expires_at"))]
    store = ContentAddressedPayloadStore(settings.trace_payload_path)
    deleted_at = datetime.now(UTC).isoformat()
    for payload_id in expired:
        store.delete(payload_id)
        db.execute(
            "UPDATE payload_objects SET deleted_at=?, storage_path='' WHERE payload_id=?",
            (deleted_at, payload_id),
        )
    return len(expired)


def delete_trace_payloads(trace_id: str) -> None:
    """Unlink one trace and erase bodies only when no governed reference remains."""
    rows = db.query_all(
        "SELECT DISTINCT payload_id FROM trace_payload_links WHERE trace_id=?", (trace_id,)
    )
    store = ContentAddressedPayloadStore(settings.trace_payload_path)
    orphaned: list[str] = []
    with db.transaction() as conn:
        conn.execute("DELETE FROM artifact_revisions WHERE producer_trace_id=?", (trace_id,))
        conn.execute("DELETE FROM trace_payload_links WHERE trace_id=?", (trace_id,))
        conn.execute(
            "DELETE FROM artifacts WHERE artifact_id NOT IN (SELECT artifact_id FROM artifact_revisions)"
        )
        for row in rows:
            payload_id = row["payload_id"]
            remaining = conn.execute(
                """SELECT
                     (SELECT COUNT(*) FROM trace_payload_links WHERE payload_id=?) +
                     (SELECT COUNT(*) FROM artifact_revisions WHERE payload_id=?) +
                     (SELECT COUNT(*) FROM outcome_records WHERE payload_id=?) AS count""",
                (payload_id, payload_id, payload_id),
            ).fetchone()["count"]
            if remaining == 0:
                conn.execute(
                    "UPDATE payload_objects SET deleted_at=?, storage_path='' WHERE payload_id=?",
                    (datetime.now(UTC).isoformat(), payload_id),
                )
                orphaned.append(payload_id)
    for payload_id in orphaned:
        store.delete(payload_id)


def cleanup_orphan_payload_files(grace_seconds: int = 3600) -> int:
    """Remove crash leftovers and unindexed bodies after a conservative grace period."""
    root = settings.trace_payload_path
    known = {
        row["payload_id"]
        for row in db.query_all(
            "SELECT payload_id FROM payload_objects WHERE deleted_at IS NULL"
        )
    }
    now = datetime.now(UTC).timestamp()
    removed = 0
    for path in root.iterdir():
        if not path.is_file() or now - path.stat().st_mtime < grace_seconds:
            continue
        temporary = path.name.startswith(".") and path.suffix == ".tmp"
        orphan_json = path.suffix == ".json" and path.stem not in known
        if temporary or orphan_json:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def start_payload_lifecycle_scheduler() -> None:
    global _lifecycle_task
    if _lifecycle_task is None or _lifecycle_task.done():
        _lifecycle_task = asyncio.create_task(_payload_lifecycle_loop())


async def _payload_lifecycle_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(purge_expired_payloads)
            await asyncio.to_thread(cleanup_orphan_payload_files)
        except Exception:
            logger.exception("Trace payload lifecycle sweep failed")
        await asyncio.sleep(_LIFECYCLE_INTERVAL_SECONDS)


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) <= datetime.now(UTC)
    except ValueError:
        return True
