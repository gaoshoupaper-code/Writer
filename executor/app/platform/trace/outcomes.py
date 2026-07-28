"""Trace 结束后的用户行为事实缓冲与可靠投递。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from contracts.trace.payload import PayloadGate, PayloadRejected


OutcomeType = Literal["copy", "regenerate", "adopt", "edit_diff", "human_rating"]


class OutcomeBuffer:
    """先持久化再投递；网络失败和进程重启都不会丢用户行为。"""

    def __init__(
        self,
        path: Path,
        *,
        evolution_url: str,
        notify_token: str = "",
    ) -> None:
        self.path = path
        self.evolution_url = evolution_url.rstrip("/")
        self.notify_token = notify_token
        self._lock = RLock()
        self._gate = PayloadGate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS outcome_buffer (
                    outcome_id TEXT PRIMARY KEY,
                    target_trace_id TEXT NOT NULL,
                    outcome_type TEXT NOT NULL,
                    actor_user_id TEXT,
                    payload_json TEXT,
                    capture_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outcome_pending
                    ON outcome_buffer(delivered_at, created_at);
                """
            )

    def enqueue(
        self,
        *,
        target_trace_id: str,
        outcome_type: OutcomeType,
        actor_user_id: str | None,
        payload: Any | None,
        outcome_id: str | None = None,
    ) -> tuple[str, str]:
        stable_id = outcome_id or f"outcome-{uuid4().hex}"
        capture_status = "captured"
        payload_json: str | None = None
        if payload is not None:
            try:
                payload_json = self._gate.prepare(payload).canonical_json.decode("utf-8")
            except PayloadRejected:
                capture_status = "payload_rejected"
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO outcome_buffer
                   (outcome_id, target_trace_id, outcome_type, actor_user_id,
                    payload_json, capture_status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(outcome_id) DO NOTHING""",
                (
                    stable_id, target_trace_id, outcome_type, actor_user_id,
                    payload_json, capture_status, datetime.now(UTC).isoformat(),
                ),
            )
        return stable_id, capture_status

    def deliver_pending(self, limit: int = 100) -> int:
        if not self.evolution_url:
            return 0
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM outcome_buffer WHERE delivered_at IS NULL
                   ORDER BY created_at LIMIT ?""",
                (limit,),
            ).fetchall()
        delivered = 0
        for row in rows:
            body = {
                "outcome_id": row["outcome_id"],
                "target_type": "trace",
                "target_id": row["target_trace_id"],
                "outcome_type": row["outcome_type"],
                "actor_user_id": row["actor_user_id"],
                "payload": json.loads(row["payload_json"]) if row["payload_json"] else None,
                "capture_status": row["capture_status"],
            }
            try:
                import httpx

                headers = (
                    {"X-Notify-Token": self.notify_token}
                    if self.notify_token
                    else None
                )
                response = httpx.post(
                    f"{self.evolution_url}/api/ingestion/outcomes",
                    json=body,
                    headers=headers,
                    timeout=2.0,
                )
                response.raise_for_status()
            except Exception as exc:
                with self._lock, self._connection() as conn:
                    conn.execute(
                        """UPDATE outcome_buffer
                           SET attempts=attempts+1, last_error=? WHERE outcome_id=?""",
                        (type(exc).__name__, row["outcome_id"]),
                    )
                continue
            with self._lock, self._connection() as conn:
                conn.execute(
                    """UPDATE outcome_buffer
                       SET delivered_at=?, attempts=attempts+1, last_error=NULL
                       WHERE outcome_id=?""",
                    (datetime.now(UTC).isoformat(), row["outcome_id"]),
                )
            delivered += 1
        return delivered

    def pending_count(self) -> int:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM outcome_buffer WHERE delivered_at IS NULL"
            ).fetchone()
        return int(row[0])


@lru_cache(maxsize=1)
def get_outcome_buffer() -> OutcomeBuffer:
    from app.platform.core.settings import get_settings

    settings = get_settings()
    path = Path(settings.outcome_buffer_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    evolution_url = settings.evolution_url or settings.evolution_notify_url.split(
        "/api/ingestion/", 1
    )[0]
    return OutcomeBuffer(
        path.resolve(),
        evolution_url=evolution_url,
        notify_token=settings.evolution_notify_token,
    )


_delivery_task: asyncio.Task[None] | None = None


def start_outcome_delivery() -> None:
    global _delivery_task
    if _delivery_task is None or _delivery_task.done():
        _delivery_task = asyncio.create_task(_delivery_loop())


async def stop_outcome_delivery() -> None:
    global _delivery_task
    if _delivery_task is not None:
        _delivery_task.cancel()
        try:
            await _delivery_task
        except asyncio.CancelledError:
            pass
        _delivery_task = None
    await asyncio.to_thread(get_outcome_buffer().deliver_pending)


async def _delivery_loop() -> None:
    while True:
        await asyncio.to_thread(get_outcome_buffer().deliver_pending)
        await asyncio.sleep(10)


__all__ = [
    "OutcomeBuffer", "get_outcome_buffer", "start_outcome_delivery",
    "stop_outcome_delivery",
]
