"""web / routes / documents."""
from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from articles_to_anki.cards.article_contexts import remove_article_contexts_for_document
from articles_to_anki.cards.text import (
    is_selectable_target,
    normalize_selected_text,
    now,
    safe_download_name,
)
from articles_to_anki.cards.translation import quick_translation_groups
from articles_to_anki.documents.highlights import delete_highlight_rows
from articles_to_anki.documents.pdf import add_pdf_highlights
from articles_to_anki.storage.database import get_database
from articles_to_anki.storage.files import remove_managed_files
from articles_to_anki.sync_queue import enqueue_sync_job
from articles_to_anki.web.auth import login_required, require_csrf
from articles_to_anki.web.uploads import owned_document


def register(app: Flask) -> None:
    @app.get("/article/<document_id>")
    @login_required
    def article(document_id: str) -> Response:
        document = owned_document(document_id, "pdf")
        requested_page = request.args.get("page", type=int)
        page_number = min(
            max(requested_page if requested_page is not None else document["last_page"] or 1, 1),
            max(1, document["page_count"]),
        )
        get_database().execute(
            """UPDATE documents SET last_opened_at = ?
               WHERE id = ? AND user_id = ?""",
            (now(), document["id"], session["user_id"]),
        )
        get_database().commit()
        return render_template(
            "reader.html",
            document=document,
            page_number=page_number,
        )

    @app.get("/api/quick-translation")
    @login_required
    def quick_translation() -> Response:
        word = normalize_selected_text(request.args.get("word", ""))
        if not is_selectable_target(word):
            return jsonify(error="Нужно указать до четырёх слов."), 400
        cache: dict[str, tuple[float, list[dict[str, Any]]]] = app.extensions[
            "quick_translation_cache"
        ]
        lock: threading.Lock = app.extensions["quick_translation_cache_lock"]
        key = word.casefold()
        with lock:
            cached = cache.get(key)
            if cached and cached[0] > time.monotonic():
                return jsonify(groups=cached[1])
        groups = quick_translation_groups(word)
        with lock:
            cache[key] = (time.monotonic() + 7 * 24 * 60 * 60, groups)
        return jsonify(groups=groups)

    @app.post("/article/<document_id>/read")
    @login_required
    def mark_article_read(document_id: str) -> Response:
        is_json_request = request.is_json
        require_csrf(header=is_json_request)
        document = owned_document(document_id, "pdf")
        payload = request.get_json(silent=True) if is_json_request else request.form
        read_at = now() if payload and str(payload.get("read")) == "1" else None
        get_database().execute(
            "UPDATE documents SET read_at = ? WHERE id = ? AND user_id = ?",
            (read_at, document["id"], session["user_id"]),
        )
        get_database().commit()
        if is_json_request:
            return jsonify(read=bool(read_at))
        flash("Статья отмечена прочитанной." if read_at else "Статья снова в непрочитанных.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/api/article/<document_id>/progress")
    @login_required
    def save_article_progress(document_id: str) -> Response:
        require_csrf(header=True)
        document = owned_document(document_id, "pdf")
        payload = request.get_json(silent=True) or {}
        try:
            page_number = int(payload.get("page"))
        except (TypeError, ValueError):
            return jsonify(error="Некорректная страница."), 400
        if page_number < 1 or page_number > document["page_count"]:
            return jsonify(error="Некорректная страница."), 400
        database = get_database()
        database.execute(
            """UPDATE documents SET last_page = ?, last_opened_at = ?
               WHERE id = ? AND user_id = ?""",
            (page_number, now(), document["id"], session["user_id"]),
        )
        database.commit()
        return jsonify(page=page_number)

    @app.post("/article/<document_id>/delete")
    @login_required
    def delete_article(document_id: str) -> Response:
        require_csrf()
        document = owned_document(document_id, "pdf")
        highlight_rows = get_database().execute(
            "SELECT * FROM highlights WHERE document_id = ? AND user_id = ?",
            (document_id, session["user_id"]),
        ).fetchall()
        highlight_ids = [row["id"] for row in highlight_rows]
        paths = [document["stored_path"], document["source_path"], document["text_path"]]
        paths.extend(
            str(Path(app.config["DATA_DIR"]) / "highlight_cache" / f"{highlight_id}.json")
            for highlight_id in highlight_ids
        )
        delete_highlight_rows(
            get_database(),
            highlight_rows,
            user_id=session["user_id"],
        )
        changed_cards = remove_article_contexts_for_document(
            get_database(),
            session["user_id"],
            document_id,
        )
        get_database().execute(
            "DELETE FROM documents WHERE id = ? AND user_id = ?",
            (document_id, session["user_id"]),
        )
        if changed_cards:
            account = get_database().execute(
                "SELECT state FROM anki_accounts WHERE user_id = ?",
                (session["user_id"],),
            ).fetchone()
            if account is not None and account["state"] in {"connected", "syncing", "error"}:
                enqueue_sync_job(
                    get_database(),
                    session["user_id"],
                    "article_deleted",
                    delay_seconds=0,
                )
        get_database().commit()

        with app.extensions["highlight_jobs_lock"]:
            if document_id in app.extensions["highlight_jobs"]:
                app.extensions["deleted_document_paths"][document_id] = paths
        remove_managed_files(Path(app.config["DATA_DIR"]), paths)
        return redirect(url_for("dashboard"))

    @app.get("/file/pdf/<document_id>")
    @login_required
    def pdf_file(document_id: str) -> Response:
        document = owned_document(document_id, "pdf")
        return send_file(
            document["stored_path"],
            mimetype="application/pdf",
            as_attachment=False,
            download_name=document["name"],
        )

    @app.get("/article/<document_id>/highlighted.pdf")
    @login_required
    def highlighted_pdf(document_id: str) -> Response:
        document = owned_document(document_id, "pdf")
        highlights = get_database().execute(
            """SELECT page, target, rects_json, translations_json
               FROM highlights
               WHERE document_id = ? AND user_id = ?
               ORDER BY page, created_at""",
            (document_id, session["user_id"]),
        ).fetchall()
        content = add_pdf_highlights(Path(document["stored_path"]), highlights)
        return send_file(
            io.BytesIO(content),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{safe_download_name(Path(document['name']).stem)}-highlighted.pdf",
        )
