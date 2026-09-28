"""jobs / rebuild_status."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import url_for

from articles_to_anki.cards.text import now


def _mark_rebuild_job(
    database: sqlite3.Connection,
    job_id: str,
    *,
    state: str | None = None,
    progress: int | None = None,
    stage: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    result_path: str | None = None,
    uploaded_to: str | None = None,
) -> None:
    updates: list[str] = []
    values: list[Any] = []
    for column, value in (
        ("state", state),
        ("progress", progress),
        ("stage", stage),
        ("error", error),
        ("started_at", started_at),
        ("finished_at", finished_at),
        ("result_path", result_path),
        ("uploaded_to", uploaded_to),
    ):
        if value is not None:
            updates.append(f"{column} = ?")
            values.append(value)
    values.extend((now(), job_id))
    updates.append("updated_at = ?")
    database.execute(
        f"UPDATE rebuild_jobs SET {', '.join(updates)} WHERE id = ?",
        values,
    )
    database.commit()


def prune_rebuild_jobs(database: sqlite3.Connection, user_id: int) -> None:
    """Drop old finished rebuilds (rows and files), keeping the latest three."""
    rows = database.execute(
        """SELECT id, result_path FROM rebuild_jobs
           WHERE user_id = ?
           ORDER BY created_at DESC LIMIT -1 OFFSET 3""",
        (user_id,),
    ).fetchall()
    for row in rows:
        result_path = row["result_path"]
        if result_path:
            try:
                Path(result_path).unlink(missing_ok=True)
            except OSError:
                pass
        database.execute(
            "DELETE FROM rebuild_jobs WHERE id = ? AND user_id = ?",
            (row["id"], user_id),
        )
    database.commit()


def _rebuild_job_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    state = str(row["state"])
    result_path = row["result_path"]
    return {
        "id": str(row["id"]),
        "state": state,
        "progress": int(row["progress"] or 0),
        "stage": str(row["stage"] or ""),
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "auto_upload": bool(row["uploaded_to"]),
        "download_url": (
            url_for("download_rebuilt_apkg", job_id=str(row["id"]))
            if state == "succeeded" and result_path
            else None
        ),
        "deck_name": _rebuild_deck_name_hint(row["created_at"]),
    }


def _rebuild_deck_name_hint(created_at: str | None) -> str | None:
    """Deck name the job is stamped with, derived from its start time."""
    if not created_at:
        return None
    try:
        started = datetime.fromisoformat(created_at).astimezone(UTC)
    except ValueError:
        return None
    from articles_to_anki.rebuild import _deck_name

    return _deck_name(started.replace(second=0, microsecond=0))


def _rebuild_file_stamp(value: str | None) -> str:
    """Unique per-build suffix for the downloaded APKG filename."""
    if value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            return parsed.astimezone(UTC).strftime("%Y-%m-%d-%H-%M-%S")
    return datetime.now(UTC).strftime("%Y-%m-%d-%H-%M-%S")
