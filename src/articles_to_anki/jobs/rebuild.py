"""jobs / rebuild."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask

from articles_to_anki.cards.text import now
from articles_to_anki.jobs.rebuild_status import _mark_rebuild_job, prune_rebuild_jobs
from articles_to_anki.storage.mirrors import decrypt_mirror_collection
from articles_to_anki.sync_queue import enqueue_sync_job


def enqueue_rebuild_job(
    app: Flask,
    database: sqlite3.Connection,
    user_id: int,
    *,
    selected_deck_id: int | None = None,
    job_id: str | None = None,
) -> str:
    """Create (or resume) a rebuild job row and start it in the background.

    The job row lives in the shared SQLite database, so progress survives
    page reloads and closed browsers; the finished APKG is written to
    DATA_DIR/rebuilds. Jobs left behind by a server restart are picked up
    again by resume_interrupted_rebuild_jobs — rebuilds are deterministic
    and LLM results are cached, so replaying them is cheap.
    """
    if job_id is None:
        job_id = str(uuid.uuid4())
        timestamp = now()
        database.execute(
            """INSERT INTO rebuild_jobs
               (id, user_id, state, deck_id, progress, stage,
                created_at, started_at, updated_at)
               VALUES (?, ?, 'queued', ?, 0, '', ?, NULL, ?)""",
            (job_id, user_id, selected_deck_id, timestamp, timestamp),
        )
    else:
        timestamp = now()
        row = database.execute(
            """SELECT deck_id FROM rebuild_jobs
               WHERE id = ? AND user_id = ?""",
            (job_id, user_id),
        ).fetchone()
        if row is None:
            return job_id
        selected_deck_id = row["deck_id"]
        database.execute(
            """UPDATE rebuild_jobs
               SET state = 'queued', progress = 0, stage = '', error = NULL,
                   started_at = NULL, finished_at = NULL, updated_at = ?
               WHERE id = ?""",
            (timestamp, job_id),
        )
    prune_rebuild_jobs(database, user_id)
    live_jobs: set[str] = app.extensions["rebuild_jobs"]
    with app.extensions["rebuild_jobs_lock"]:
        if job_id in live_jobs:
            return job_id
        live_jobs.add(job_id)
    executor: ThreadPoolExecutor = app.extensions["rebuild_executor"]
    if app.config["REBUILD_INLINE"]:
        run_rebuild_job(
            app,
            job_id,
            user_id,
            selected_deck_id,
            database=database,
        )
    else:
        executor.submit(
            run_rebuild_job,
            app,
            job_id,
            user_id,
            selected_deck_id,
        )
    return job_id


def run_rebuild_job(
    app: Flask,
    job_id: str,
    user_id: int,
    selected_deck_id: int | None,
    database: sqlite3.Connection | None = None,
) -> None:
    """Execute one rebuild job and persist its outcome in rebuild_jobs."""
    close_database = database is None
    if database is None:
        database = sqlite3.connect(app.config["DATABASE"], timeout=30)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
    result: Path | None = None
    error: str | None = None
    try:
        _mark_rebuild_job(
            database,
            job_id,
            state="running",
            progress=5,
            stage="Начинаю сборку",
            started_at=now(),
        )
        from articles_to_anki.rebuild import build_rebuilt_deck_apkg

        with tempfile.TemporaryDirectory(
            prefix="anki-papers-rebuild-"
        ) as temporary_name:
            temporary = Path(temporary_name)
            source_path: Path | None = None
            if selected_deck_id is not None:
                mirror = decrypt_mirror_collection(database, user_id)
                if mirror is None:
                    raise RuntimeError(
                        "Свежее зеркало AnkiWeb недоступно: синхронизируйтесь "
                        "и повторите сборку."
                    )
                mirror_path = temporary / "mirror-collection.anki2"
                mirror_path.write_bytes(mirror)
                source_path = mirror_path
            content = build_rebuilt_deck_apkg(
                database,
                user_id,
                data_dir=Path(app.config["DATA_DIR"]),
                source_path=source_path,
                selected_deck_id=selected_deck_id,
                progress=lambda percent, stage: _mark_rebuild_job(
                    database,
                    job_id,
                    progress=percent,
                    stage=stage,
                ),
            )
        result_dir = Path(app.config["DATA_DIR"]) / "rebuilds" / str(user_id)
        result_dir.mkdir(parents=True, exist_ok=True)
        result = result_dir / f"{job_id}.apkg"
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{job_id}-",
                suffix=".apkg",
                dir=result_dir,
                delete=False,
            ) as temporary_handle:
                temporary_path = Path(temporary_handle.name)
                temporary_handle.write(content)
            os.replace(temporary_path, result)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
    except Exception:
        app.logger.exception("Deck rebuild job %s failed", job_id)
        error = (
            "Не удалось пересобрать колоду. Синхронизируйтесь заново "
            "и попробуйте ещё раз."
        )
    finally:
        if error is None:
            _mark_rebuild_job(
                database,
                job_id,
                state="succeeded",
                progress=100,
                stage="Готово",
                finished_at=now(),
                result_path=str(result) if result else None,
            )
            if selected_deck_id is not None and _has_ankiweb_mirror(database, user_id):
                _mark_rebuild_job(database, job_id, uploaded_to="queued")
                enqueue_sync_job(database, user_id, "rebuild_import", delay_seconds=0)
                database.commit()
        else:
            _mark_rebuild_job(
                database,
                job_id,
                state="failed",
                error=error,
                finished_at=now(),
            )
            if result is not None:
                result.unlink(missing_ok=True)
        if close_database:
            database.close()
        with app.extensions["rebuild_jobs_lock"]:
            app.extensions["rebuild_jobs"].discard(job_id)


def _has_ankiweb_mirror(database: sqlite3.Connection, user_id: int) -> bool:
    row = database.execute(
        "SELECT 1 FROM anki_accounts WHERE user_id = ? AND mirror_path IS NOT NULL",
        (user_id,),
    ).fetchone()
    return row is not None
