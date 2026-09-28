"""web / routes / highlights."""
from __future__ import annotations

import json
import uuid

from flask import Flask, Response, abort, jsonify, redirect, request, session, url_for

from articles_to_anki.cards.enrichment import enqueue_reader_highlight_enrichment
from articles_to_anki.cards.text import (
    is_selectable_target,
    normalize_selected_text,
    now,
)
from articles_to_anki.documents.highlights import (
    clean_highlight_rects,
    delete_highlight_rows,
    highlight_json,
)
from articles_to_anki.storage.database import get_database
from articles_to_anki.storage.migrations import card_context_key
from articles_to_anki.web.auth import login_required, require_csrf
from articles_to_anki.web.uploads import owned_document


def register(app: Flask) -> None:
    @app.route("/api/article/<document_id>/highlights", methods=["GET", "POST"])
    @login_required
    def article_highlights(document_id: str) -> Response:
        document = owned_document(document_id, "pdf")
        database = get_database()

        if request.method == "GET":
            rows = database.execute(
                """SELECT * FROM highlights
                   WHERE document_id = ? AND user_id = ?
                   ORDER BY created_at""",
                (document_id, session["user_id"]),
            ).fetchall()
            current = database.execute(
                "SELECT highlight_status FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            for row in rows:
                if row["status"] == "pending" and row["source"] == "reader":
                    enqueue_reader_highlight_enrichment(
                        app,
                        highlight_id=row["id"],
                        user_id=session["user_id"],
                        document_id=document_id,
                    )
            return jsonify(
                highlights=[highlight_json(row) for row in rows],
                processing_status=current["highlight_status"],
            )

        require_csrf(header=True)
        payload = request.get_json(silent=True) or {}
        highlight_id = str(payload.get("id", ""))
        try:
            if str(uuid.UUID(highlight_id)) != highlight_id:
                raise ValueError
        except ValueError:
            return jsonify(error="Некорректный ID выделения."), 400
        target = normalize_selected_text(str(payload.get("target", "")))
        sentence = str(payload.get("sentence", "")).strip()
        page_number = payload.get("page")
        try:
            page_number = int(page_number)
            rects = clean_highlight_rects(payload.get("rects"))
        except (KeyError, OverflowError, TypeError, ValueError):
            return jsonify(error="Некорректные координаты выделения."), 400
        if not is_selectable_target(target) or not sentence or len(sentence) > 1200:
            return jsonify(error="Нужно выделить до четырёх слов."), 400
        if page_number < 1 or page_number > document["page_count"]:
            return jsonify(error="Некорректная страница."), 400

        rects_json = json.dumps(rects, separators=(",", ":"))
        timestamp = now()
        database.execute(
            """INSERT OR IGNORE INTO highlights
               (id, user_id, document_id, target, sentence, page, rects_json,
                translations_json, replacement, alternatives_json, status,
                error, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '', '[]', 'pending', NULL, 'reader', ?, ?)""",
            (
                highlight_id,
                session["user_id"],
                document_id,
                target,
                sentence,
                page_number,
                rects_json,
                timestamp,
                timestamp,
            ),
        )
        database.commit()
        row = database.execute(
            """SELECT * FROM highlights
               WHERE id = ? AND user_id = ? AND document_id = ?""",
            (highlight_id, session["user_id"], document_id),
        ).fetchone()
        if row is None:
            row = database.execute(
                """SELECT * FROM highlights
                   WHERE user_id = ? AND document_id = ? AND page = ? AND rects_json = ?""",
                (session["user_id"], document_id, page_number, rects_json),
            ).fetchone()
        if row is None:
            abort(500, "Не удалось сохранить выделение")
        if row["status"] == "ready":
            return jsonify(highlight=highlight_json(row))
        enqueue_reader_highlight_enrichment(
            app,
            highlight_id=row["id"],
            user_id=session["user_id"],
            document_id=document_id,
        )
        current = database.execute(
            "SELECT * FROM highlights WHERE id = ? AND user_id = ?",
            (row["id"], session["user_id"]),
        ).fetchone()
        if current is None:
            return jsonify(discarded_highlight_id=row["id"])
        return jsonify(highlight=highlight_json(current))

    @app.delete("/api/article/<document_id>/highlights/<highlight_id>")
    @login_required
    def delete_article_highlight(document_id: str, highlight_id: str) -> Response:
        owned_document(document_id, "pdf")
        require_csrf(header=True)
        row = get_database().execute(
            """SELECT * FROM highlights
               WHERE id = ? AND document_id = ? AND user_id = ?""",
            (highlight_id, document_id, session["user_id"]),
        ).fetchone()
        if row is None:
            abort(404)
        delete_highlight_rows(
            get_database(),
            [row],
            user_id=session["user_id"],
        )
        return jsonify(ok=True, deleted_highlight_id=highlight_id)

    @app.post("/cards/<card_id>/delete")
    @login_required
    def delete_card(card_id: str) -> Response:
        require_csrf()
        database = get_database()
        card = database.execute(
            "SELECT * FROM cards WHERE id = ? AND user_id = ?",
            (card_id, session["user_id"]),
        ).fetchone()
        if card is None:
            abort(404)
        rows = database.execute(
            """SELECT highlights.* FROM highlights
               JOIN card_highlights ON card_highlights.highlight_id = highlights.id
               WHERE card_highlights.card_id = ? AND highlights.user_id = ?""",
            (card_id, session["user_id"]),
        ).fetchall()
        if not rows:
            rows = [
                row
                for row in database.execute(
                    "SELECT * FROM highlights WHERE user_id = ?",
                    (session["user_id"],),
                ).fetchall()
                if card_context_key(row) == card_context_key(card)
            ]
        delete_highlight_rows(database, rows, user_id=session["user_id"])
        database.execute(
            "DELETE FROM cards WHERE id = ? AND user_id = ?",
            (card_id, session["user_id"]),
        )
        database.commit()
        return redirect(url_for("dashboard"))
