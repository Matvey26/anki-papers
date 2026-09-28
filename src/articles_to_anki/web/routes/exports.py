"""web / routes / exports."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    request,
    send_file,
    session,
    url_for,
)

from articles_to_anki.cards.rendering import cards_to_csv
from articles_to_anki.cards.text import safe_download_name
from articles_to_anki.jobs.rebuild import enqueue_rebuild_job
from articles_to_anki.jobs.rebuild_status import (
    _rebuild_file_stamp,
    _rebuild_job_payload,
)
from articles_to_anki.storage.database import get_database
from articles_to_anki.storage.files import replace_managed_file
from articles_to_anki.web.auth import login_required, require_csrf
from articles_to_anki.web.exports import mark_exported, pending_cards
from articles_to_anki.web.uploads import owned_document


def register(app: Flask) -> None:
    @app.post("/export/csv")
    @login_required
    def export_csv() -> Response:
        require_csrf()
        cards = pending_cards("csv_exported_at")
        if not cards:
            flash("Новых карточек для CSV нет.", "error")
            return redirect(url_for("dashboard"))
        content = cards_to_csv(cards)
        mark_exported(cards, "csv_exported_at")
        return Response(
            content,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="anki-new-{datetime.now(UTC).date()}.csv"'},
        )

    @app.post("/export/apkg")
    @login_required
    def export_apkg() -> Response:
        require_csrf()
        deck_id = request.form.get("deck_id", "")
        deck = owned_document(deck_id, "apkg")
        pending = pending_cards("apkg_exported_at")
        stem = Path(deck["name"]).stem
        if not pending:
            return send_file(
                deck["stored_path"],
                mimetype="application/octet-stream",
                as_attachment=True,
                download_name=f"{safe_download_name(stem)}-updated.apkg",
            )
        cards = get_database().execute(
            """SELECT cards.*, documents.name AS document_name
               FROM cards JOIN documents ON documents.id = cards.document_id
               WHERE cards.user_id = ? ORDER BY cards.created_at""",
            (session["user_id"],),
        ).fetchall()
        from articles_to_anki.apkg import merge

        try:
            with tempfile.TemporaryDirectory(
                prefix="anki-papers-export-"
            ) as temporary_name:
                temporary = Path(temporary_name)
                csv_path = temporary / "new.csv"
                csv_path.write_bytes(cards_to_csv(cards))
                destination = temporary / "updated.apkg"
                merge(
                    Path(deck["stored_path"]),
                    destination,
                    [csv_path],
                    temporary / "combined.csv",
                )
                content = destination.read_bytes()
                replace_managed_file(Path(deck["stored_path"]), content)
                get_database().execute(
                    "UPDATE documents SET size = ? WHERE id = ? AND user_id = ?",
                    (len(content), deck["id"], session["user_id"]),
                )
                get_database().commit()
        except Exception:
            app.logger.exception("APKG export failed")
            flash(
                "Не удалось обновить эту колоду. Загрузите APKG, экспортированный Anki.",
                "error",
            )
            return redirect(url_for("dashboard"))
        mark_exported(cards, "apkg_exported_at")
        return Response(
            content,
            mimetype="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe_download_name(stem)}-updated.apkg"'},
        )

    @app.post("/export/rebuild")
    @login_required
    def export_rebuild_apkg() -> Response:
        require_csrf()
        database = get_database()
        user_id = session["user_id"]
        account = database.execute(
            "SELECT * FROM anki_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
        selected_deck_id: int | None = None
        deck_value = request.form.get("deck_id", "")
        if deck_value:
            try:
                selected_deck_id = int(deck_value)
                decks = json.loads(account["available_decks_json"]) if account else []
                next(deck for deck in decks if int(deck["id"]) == selected_deck_id)
            except (StopIteration, TypeError, ValueError, json.JSONDecodeError):
                flash(
                    "Выбранной колоды нет в аккаунте AnkiWeb. Синхронизируйтесь "
                    "заново и повторите сборку.",
                    "error",
                )
                return redirect(url_for("settings"))
        active = database.execute(
            """SELECT id FROM rebuild_jobs
               WHERE user_id = ? AND state IN ('queued', 'running') LIMIT 1""",
            (user_id,),
        ).fetchone()
        if active is not None:
            flash(
                "Пересборка уже идёт. Её можно оставить: файл появится на "
                "этой странице, когда сборка закончится.",
                "error",
            )
            return redirect(url_for("settings"))
        try:
            enqueue_rebuild_job(
                app,
                database,
                user_id,
                selected_deck_id=selected_deck_id,
            )
        except sqlite3.IntegrityError:
            database.rollback()
            flash(
                "Пересборка уже идёт. Её можно оставить: файл появится на "
                "этой странице, когда сборка закончится.",
                "error",
            )
            return redirect(url_for("settings"))
        database.commit()
        flash(
            "Сборка колоды запущена. Можно закрыть страницу — готовый файл "
            "появится здесь.",
            "success",
        )
        return redirect(url_for("settings") + "#rebuild")

    @app.get("/api/rebuild/status")
    @login_required
    def rebuild_status() -> Response:
        row = get_database().execute(
            """SELECT * FROM rebuild_jobs
               WHERE user_id = ? ORDER BY created_at DESC LIMIT 1""",
            (session["user_id"],),
        ).fetchone()
        response = jsonify(rebuild_job=_rebuild_job_payload(row))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/export/rebuild/download/<job_id>")
    @login_required
    def download_rebuilt_apkg(job_id: str) -> Response:
        row = get_database().execute(
            "SELECT * FROM rebuild_jobs WHERE id = ? AND user_id = ?",
            (job_id, session["user_id"]),
        ).fetchone()
        if row is None or row["state"] != "succeeded" or not row["result_path"]:
            abort(404)
        return send_file(
            row["result_path"],
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=f"anki-papers-rebuild-{_rebuild_file_stamp(row['created_at'])}.apkg",
        )
