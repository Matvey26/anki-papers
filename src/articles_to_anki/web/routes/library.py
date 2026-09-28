"""web / routes / library."""
from __future__ import annotations

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from articles_to_anki.documents.processing import enqueue_document_processing
from articles_to_anki.storage.database import get_database
from articles_to_anki.sync_ui import build_sync_status
from articles_to_anki.web.auth import login_required, require_csrf
from articles_to_anki.web.exports import reconcile_apkg_exports
from articles_to_anki.web.uploads import owned_document, save_document


def register(app: Flask) -> None:
    @app.get("/dashboard")
    @login_required
    def dashboard() -> Response:
        user_id = session["user_id"]
        db = get_database()
        documents = db.execute(
            """SELECT documents.*,
                      (SELECT COUNT(*) FROM highlights
                       WHERE highlights.document_id = documents.id
                         AND highlights.user_id = documents.user_id) AS highlight_count
               FROM documents
               WHERE documents.user_id = ? AND documents.kind = 'pdf'
               ORDER BY documents.last_opened_at DESC, documents.created_at DESC""",
            (user_id,),
        ).fetchall()
        cards = db.execute(
            """SELECT cards.*, documents.name AS document_name
               FROM cards JOIN documents ON documents.id = cards.document_id
               WHERE cards.user_id = ? ORDER BY cards.created_at DESC""",
            (user_id,),
        ).fetchall()
        anki_account = db.execute(
            "SELECT * FROM anki_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
        active_sync = db.execute(
            """SELECT * FROM sync_jobs
               WHERE user_id = ? AND state IN ('queued', 'running')
               ORDER BY created_at LIMIT 1""",
            (user_id,),
        ).fetchone()
        latest_sync = db.execute(
            "SELECT * FROM sync_jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        pending_words = sum(card["anki_synced_at"] is None for card in cards)
        return render_template(
            "dashboard.html",
            documents=documents,
            cards=cards,
            anki_account=anki_account,
            sync_status=build_sync_status(
                anki_account, active_sync, latest_sync, pending_words
            ),
        )

    @app.post("/upload/pdf")
    @login_required
    def upload_pdf() -> Response:
        require_csrf()
        upload = request.files.get("file")
        try:
            document_id = save_document(upload, "pdf")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        else:
            if app.config["AUTO_PROCESS_UPLOADS"]:
                enqueue_document_processing(app, document_id, session["user_id"])
                flash("Статья загружена. Хайлайты обрабатываются в фоне.", "success")
            else:
                flash("Статья загружена.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/upload/apkg")
    @login_required
    def upload_apkg() -> Response:
        require_csrf()
        upload = request.files.get("file")
        try:
            document_id = save_document(upload, "apkg")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        else:
            try:
                reconcile_apkg_exports(owned_document(document_id, "apkg"))
            except Exception:
                app.logger.warning("Could not inspect uploaded APKG", exc_info=True)
            flash("Колода загружена.", "success")
        return redirect(url_for("dashboard"))
