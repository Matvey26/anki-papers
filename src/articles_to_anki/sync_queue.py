from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def enqueue_sync_job(
    database: sqlite3.Connection,
    user_id: int,
    reason: str,
    *,
    delay_seconds: int = 30,
) -> str:
    timestamp = datetime.now(UTC)
    run_after = (timestamp + timedelta(seconds=max(delay_seconds, 0))).isoformat()
    existing = database.execute(
        """SELECT id FROM sync_jobs
           WHERE user_id = ? AND state = 'queued'
           ORDER BY created_at LIMIT 1""",
        (user_id,),
    ).fetchone()
    if existing:
        database.execute(
            """UPDATE sync_jobs
               SET reason = ?, run_after = ?, updated_at = ?
               WHERE id = ?""",
            (reason, run_after, timestamp.isoformat(), existing["id"]),
        )
        return str(existing["id"])
    job_id = str(uuid.uuid4())
    database.execute(
        """INSERT INTO sync_jobs
           (id, user_id, reason, state, attempts, run_after, created_at, updated_at)
           VALUES (?, ?, ?, 'queued', 0, ?, ?, ?)""",
        (job_id, user_id, reason, run_after, timestamp.isoformat(), timestamp.isoformat()),
    )
    return job_id
