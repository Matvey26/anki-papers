"""web / hooks."""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from flask import Flask, Response, request, session

from articles_to_anki.cards.text import word_count_label
from articles_to_anki.documents.processing import enqueue_document_processing
from articles_to_anki.jobs.rebuild import enqueue_rebuild_job
from articles_to_anki.storage.database import get_database
from articles_to_anki.sync_ui import (
    build_sync_status,
    format_sync_time,
    sync_error_text,
    sync_job_reason,
    sync_job_state,
)
from articles_to_anki.web.auth import csrf_token


def register(app: Flask) -> None:
    @app.context_processor
    def template_context() -> dict[str, Any]:
        context: dict[str, Any] = {
            "csrf_token": csrf_token,
            "word_count_label": word_count_label,
            "format_sync_time": format_sync_time,
            "sync_error_text": sync_error_text,
            "sync_job_reason": sync_job_reason,
            "sync_job_state": sync_job_state,
            "header_sync": None,
        }
        user_id = session.get("user_id")
        if user_id is None:
            return context

        database = get_database()
        account = database.execute(
            "SELECT * FROM anki_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
        active_job = database.execute(
            """SELECT * FROM sync_jobs
               WHERE user_id = ? AND state IN ('queued', 'running')
               ORDER BY created_at LIMIT 1""",
            (user_id,),
        ).fetchone()
        latest_job = database.execute(
            "SELECT * FROM sync_jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        pending_words = database.execute(
            "SELECT COUNT(*) FROM cards WHERE user_id = ? AND anki_synced_at IS NULL",
            (user_id,),
        ).fetchone()[0]
        status = build_sync_status(account, active_job, latest_job, pending_words)
        state = account["state"] if account else None

        header_sync: dict[str, Any] = {
            "tone": status["tone"],
            "title": status["title"],
            "active": status["active"],
        }
        if status["active"]:
            header_sync.update(kind="disabled", label="Синхронизация…")
        elif state in {"connected", "error"}:
            header_sync.update(kind="submit", label="Синхронизировать")
        elif state == "awaiting_deck":
            header_sync.update(kind="link", label="Выбрать колоду")
        elif state == "needs_reconnect":
            header_sync.update(kind="link", label="Исправить синхронизацию")
        else:
            header_sync.update(kind="link", label="Подключить AnkiWeb")
        context["header_sync"] = header_sync
        return context

    @app.before_request
    def resume_interrupted_highlight_jobs() -> None:
        if not app.config["AUTO_PROCESS_UPLOADS"] or app.extensions["highlight_resume_done"]:
            return
        with app.extensions["highlight_jobs_lock"]:
            if app.extensions["highlight_resume_done"]:
                return
            app.extensions["highlight_resume_done"] = True
        rows = get_database().execute(
            """SELECT id, user_id FROM documents
               WHERE kind = 'pdf' AND highlight_status IN ('queued', 'processing')"""
        ).fetchall()
        for row in rows:
            enqueue_document_processing(app, row["id"], row["user_id"])

    @app.before_request
    def resume_interrupted_rebuild_jobs() -> None:
        live_jobs: set[str] = app.extensions["rebuild_jobs"]
        with app.extensions["rebuild_jobs_lock"]:
            checked_at = time.monotonic()
            if checked_at - app.extensions.get("rebuild_resume_checked_at", float("-inf")) < 5:
                return
            app.extensions["rebuild_resume_checked_at"] = checked_at
            stale_running_since = (
                datetime.now(UTC) - timedelta(minutes=5)
            ).isoformat()
            rows = get_database().execute(
                """SELECT id, user_id, state, updated_at FROM rebuild_jobs
                   WHERE state = 'queued'
                      OR (state = 'running' AND updated_at < ?)""",
                (stale_running_since,),
            ).fetchall()
            resumable = [
                (str(row["id"]), int(row["user_id"]))
                for row in rows
                if str(row["id"]) not in live_jobs
            ]
        for job_id, user_id in resumable:
            enqueue_rebuild_job(app, get_database(), user_id, job_id=job_id)

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; script-src 'self'; "
            "style-src 'self'; object-src 'self'; base-uri 'self'; frame-ancestors 'none'",
        )
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
