from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import functools
import html
import io
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight
from pypdf.generic import ArrayObject, FloatObject, NameObject, TextStringObject
from .enrich import DEFAULT_MODEL, enrich_targets, load_env_file
from .extract import RECALL_PLACEHOLDER, extract_highlights
from .models import TargetContext

USERNAME_RE = re.compile(r"^[\w.\-]{3,32}$", re.UNICODE)
WORD_RE = re.compile(r"^[\w]+(?:['’\-][\w]+)*$", re.UNICODE)


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    load_env_file(Path.cwd() / ".env")
    app = Flask(__name__)
    data_dir = Path(os.environ.get("ANKI_PAPERS_DATA_DIR", "data")).resolve()
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("APP_SECRET") or secrets.token_hex(32),
        DATA_DIR=data_dir,
        DATABASE=data_dir / "app.sqlite3",
        MAX_CONTENT_LENGTH=85 * 1024 * 1024,
        AUTO_PROCESS_UPLOADS=True,
        PROCESS_DOCUMENTS_INLINE=False,
    )
    if test_config:
        app.config.update(test_config)
    Path(app.config["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    init_database(app)
    app.extensions["highlight_executor"] = ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="pdf-highlights",
    )
    app.extensions["highlight_jobs"] = set()
    app.extensions["highlight_jobs_lock"] = threading.Lock()
    app.extensions["highlight_resume_done"] = False

    app.teardown_appcontext(close_database)
    app.context_processor(lambda: {"csrf_token": csrf_token})

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

    @app.get("/health")
    def health() -> Response:
        return jsonify(ok=True)

    @app.get("/")
    def index() -> Response:
        if "user_id" not in session:
            return redirect(url_for("login"))
        return redirect(url_for("dashboard"))

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response:
        if request.method == "POST":
            require_csrf()
            username = request.form.get("username", "").strip()
            if not USERNAME_RE.fullmatch(username):
                flash("Логин: 3–32 символа; буквы, цифры, точка, дефис или подчёркивание.", "error")
            else:
                database = get_database()
                database.execute(
                    "INSERT OR IGNORE INTO users (username, created_at) VALUES (?, ?)",
                    (username, now()),
                )
                database.commit()
                user = database.execute(
                    "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
                    (username,),
                ).fetchone()
                if user is None:
                    abort(500, "Не удалось создать профиль")
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                return redirect(url_for("dashboard"))
        return render_template("auth.html")

    @app.route("/register", methods=["GET", "POST"])
    def register() -> Response:
        return login()

    @app.post("/logout")
    @login_required
    def logout() -> Response:
        require_csrf()
        session.clear()
        return redirect(url_for("login"))

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
               ORDER BY documents.created_at DESC""",
            (user_id,),
        ).fetchall()
        decks = db.execute(
            "SELECT * FROM documents WHERE user_id = ? AND kind = 'apkg' ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        cards = db.execute(
            """SELECT cards.*, documents.name AS document_name
               FROM cards JOIN documents ON documents.id = cards.document_id
               WHERE cards.user_id = ? ORDER BY cards.created_at DESC""",
            (user_id,),
        ).fetchall()
        return render_template(
            "dashboard.html",
            documents=documents,
            decks=decks,
            cards=cards,
            new_csv=sum(card["csv_exported_at"] is None for card in cards) * 2,
            new_apkg=sum(card["apkg_exported_at"] is None for card in cards) * 2,
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
            save_document(upload, "apkg")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        else:
            flash("Колода загружена.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/article/<document_id>")
    @login_required
    def article(document_id: str) -> Response:
        document = owned_document(document_id, "pdf")
        page_number = min(
            max(request.args.get("page", 1, type=int), 1),
            max(1, document["page_count"]),
        )
        return render_template(
            "reader.html",
            document=document,
            page_number=page_number,
        )

    @app.post("/article/<document_id>/read")
    @login_required
    def mark_article_read(document_id: str) -> Response:
        require_csrf()
        document = owned_document(document_id, "pdf")
        read_at = now() if request.form.get("read") == "1" else None
        get_database().execute(
            "UPDATE documents SET read_at = ? WHERE id = ? AND user_id = ?",
            (read_at, document["id"], session["user_id"]),
        )
        get_database().commit()
        flash("Статья отмечена прочитанной." if read_at else "Статья снова в непрочитанных.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/article/<document_id>/process-highlights")
    @login_required
    def process_article_highlights(document_id: str) -> Response:
        require_csrf()
        document = owned_document(document_id, "pdf")
        get_database().execute(
            """UPDATE documents
               SET highlight_status = 'queued', highlight_error = NULL
               WHERE id = ? AND user_id = ?""",
            (document["id"], session["user_id"]),
        )
        get_database().commit()
        enqueue_document_processing(app, document["id"], session["user_id"])
        flash("Обработка хайлайтов запущена.", "success")
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

    @app.route("/api/article/<document_id>/highlights", methods=["GET", "POST"])
    @login_required
    def article_highlights(document_id: str) -> Response:
        document = owned_document(document_id, "pdf")
        database = get_database()

        def fail_highlight(highlight_id: str, message: str, status_code: int) -> tuple[Response, int]:
            timestamp = now()
            database.execute(
                """UPDATE highlights SET status = 'failed', error = ?, updated_at = ?
                   WHERE id = ? AND user_id = ?""",
                (message, timestamp, highlight_id, session["user_id"]),
            )
            database.commit()
            failed = database.execute(
                "SELECT * FROM highlights WHERE id = ? AND user_id = ?",
                (highlight_id, session["user_id"]),
            ).fetchone()
            return jsonify(error=message, highlight=highlight_json(failed)), status_code

        if request.method == "GET":
            rows = database.execute(
                """SELECT * FROM highlights
                   WHERE document_id = ? AND user_id = ?
                   ORDER BY created_at""",
                (document_id, session["user_id"]),
            ).fetchall()
            current = database.execute(
                "SELECT highlight_status, highlight_error FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            return jsonify(
                highlights=[highlight_json(row) for row in rows],
                processing_status=current["highlight_status"],
                processing_error=current["highlight_error"],
            )

        require_csrf(header=True)
        payload = request.get_json(silent=True) or {}
        highlight_id = str(payload.get("id", ""))
        try:
            if str(uuid.UUID(highlight_id)) != highlight_id:
                raise ValueError
        except ValueError:
            return jsonify(error="Некорректный ID выделения."), 400
        target = str(payload.get("target", "")).strip()
        sentence = str(payload.get("sentence", "")).strip()
        page_number = payload.get("page")
        try:
            page_number = int(page_number)
            rects = clean_highlight_rects(payload.get("rects"))
        except (KeyError, OverflowError, TypeError, ValueError):
            return jsonify(error="Некорректные координаты выделения."), 400
        if not is_single_word(target) or not sentence or len(sentence) > 1200:
            return jsonify(error="Нужно выделить одно слово."), 400
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

        try:
            ready = enrich_highlight_row(
                app,
                database,
                row,
                user_id=session["user_id"],
                document_id=document_id,
            )
        except MissingApiKeyError:
            return fail_highlight(row["id"], "OPENROUTER_API_KEY не настроен.", 503)
        except Exception:
            app.logger.exception("Automatic highlight enrichment failed")
            return fail_highlight(row["id"], "Автоперевод временно недоступен.", 502)
        return jsonify(highlight=highlight_json(ready))

    @app.post("/cards/<card_id>/delete")
    @login_required
    def delete_card(card_id: str) -> Response:
        require_csrf()
        get_database().execute(
            "DELETE FROM cards WHERE id = ? AND user_id = ?",
            (card_id, session["user_id"]),
        )
        get_database().commit()
        flash("Карточка удалена.", "success")
        return redirect(url_for("dashboard"))

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
        cards = pending_cards("apkg_exported_at")
        if not cards:
            flash("Новых карточек для APKG нет.", "error")
            return redirect(url_for("dashboard"))
        from .apkg import merge

        with tempfile.TemporaryDirectory(prefix="anki-papers-export-") as temporary_name:
            temporary = Path(temporary_name)
            csv_path = temporary / "new.csv"
            csv_path.write_bytes(cards_to_csv(cards))
            destination = temporary / "updated.apkg"
            merge(Path(deck["stored_path"]), destination, [csv_path], temporary / "combined.csv")
            content = destination.read_bytes()
        mark_exported(cards, "apkg_exported_at")
        stem = Path(deck["name"]).stem
        return Response(
            content,
            mimetype="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe_download_name(stem)}-updated.apkg"'},
        )

    def save_document(upload: Any, kind: str) -> str:
        if upload is None or not getattr(upload, "filename", ""):
            raise ValueError("Выберите файл.")
        extension = ".pdf" if kind == "pdf" else ".apkg"
        if not upload.filename.lower().endswith(extension):
            raise ValueError(f"Нужен файл {extension.upper()}.")
        head = upload.stream.read(5)
        upload.stream.seek(0)
        if kind == "pdf" and head != b"%PDF-":
            raise ValueError("Файл не похож на PDF.")
        if kind == "apkg" and not head.startswith(b"PK"):
            raise ValueError("Файл не похож на APKG.")
        document_id = str(uuid.uuid4())
        user_dir = Path(app.config["DATA_DIR"]) / "uploads" / str(session["user_id"])
        user_dir.mkdir(parents=True, exist_ok=True)
        stored_path = user_dir / f"{document_id}{extension}"
        source_path = user_dir / f"{document_id}.source.pdf" if kind == "pdf" else None
        upload_path = source_path or stored_path
        upload.save(upload_path)
        size_limit = 50 * 1024 * 1024 if kind == "pdf" else 80 * 1024 * 1024
        if upload_path.stat().st_size > size_limit:
            upload_path.unlink(missing_ok=True)
            raise ValueError(f"Файл больше {size_limit // 1024 // 1024} МБ.")
        page_count = 0
        text_path: Path | None = None
        try:
            if kind == "pdf":
                reader = PdfReader(upload_path)
                page_count = len(reader.pages)
                write_pdf_without_native_highlights(upload_path, stored_path)
            get_database().execute(
                """INSERT INTO documents
                   (id, user_id, kind, name, stored_path, source_path, text_path,
                    page_count, size, highlight_status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    document_id,
                    session["user_id"],
                    kind,
                    Path(upload.filename).name,
                    str(stored_path),
                    str(source_path) if source_path else None,
                    str(text_path) if text_path else None,
                    page_count,
                    upload_path.stat().st_size,
                    "queued" if kind == "pdf" and app.config["AUTO_PROCESS_UPLOADS"] else "idle",
                    now(),
                ),
            )
            get_database().commit()
        except Exception:
            stored_path.unlink(missing_ok=True)
            if text_path:
                text_path.unlink(missing_ok=True)
            if source_path:
                source_path.unlink(missing_ok=True)
            raise
        return document_id

    def owned_document(document_id: str, kind: str) -> sqlite3.Row:
        row = get_database().execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ? AND kind = ?",
            (document_id, session["user_id"], kind),
        ).fetchone()
        if row is None:
            abort(404)
        return row

    def pending_cards(column: str) -> list[sqlite3.Row]:
        if column not in {"csv_exported_at", "apkg_exported_at"}:
            raise ValueError("Unknown export column")
        return get_database().execute(
            f"""SELECT cards.*, documents.name AS document_name
                FROM cards JOIN documents ON documents.id = cards.document_id
                WHERE cards.user_id = ? AND cards.{column} IS NULL
                ORDER BY cards.created_at""",
            (session["user_id"],),
        ).fetchall()

    def mark_exported(cards: list[sqlite3.Row], column: str) -> None:
        if column not in {"csv_exported_at", "apkg_exported_at"}:
            raise ValueError("Unknown export column")
        timestamp = now()
        get_database().executemany(
            f"UPDATE cards SET {column} = ? WHERE id = ? AND user_id = ?",
            [(timestamp, card["id"], session["user_id"]) for card in cards],
        )
        get_database().commit()

    return app


def login_required(view: Callable[..., Response]) -> Callable[..., Response]:
    @functools.wraps(view)
    def wrapped(**kwargs: Any) -> Response:
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped


def get_database() -> sqlite3.Connection:
    if "database" not in g:
        connection = sqlite3.connect(current_app.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.database = connection
    return g.database


def close_database(_: BaseException | None = None) -> None:
    connection = g.pop("database", None)
    if connection is not None:
        connection.close()


def init_database(app: Flask) -> None:
    connection = sqlite3.connect(app.config["DATABASE"])
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('pdf', 'apkg')),
                name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                text_path TEXT,
                page_count INTEGER NOT NULL DEFAULT 0,
                size INTEGER NOT NULL,
                read_at TEXT,
                source_path TEXT,
                highlight_status TEXT NOT NULL DEFAULT 'idle'
                    CHECK(highlight_status IN ('idle', 'queued', 'processing', 'ready', 'failed')),
                highlight_error TEXT,
                highlight_processed_at TEXT,
                imported_highlight_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_documents_user_kind
                ON documents(user_id, kind, created_at);
            CREATE TABLE IF NOT EXISTS highlights (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                target TEXT NOT NULL,
                sentence TEXT NOT NULL,
                page INTEGER NOT NULL,
                rects_json TEXT NOT NULL,
                translations_json TEXT NOT NULL DEFAULT '[]',
                replacement TEXT NOT NULL DEFAULT '',
                alternatives_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'ready', 'failed')),
                error TEXT,
                source TEXT NOT NULL DEFAULT 'reader'
                    CHECK(source IN ('reader', 'pdf_import')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, document_id, page, rects_json)
            );
            CREATE INDEX IF NOT EXISTS idx_highlights_document
                ON highlights(user_id, document_id, page, created_at);
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                target TEXT NOT NULL,
                target_normalized TEXT NOT NULL,
                sentence TEXT NOT NULL,
                page INTEGER NOT NULL,
                translations_json TEXT NOT NULL,
                replacement TEXT NOT NULL,
                alternatives_json TEXT NOT NULL,
                csv_exported_at TEXT,
                apkg_exported_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, target_normalized)
            );
            CREATE INDEX IF NOT EXISTS idx_cards_user_created
                ON cards(user_id, created_at);
            """
        )
        document_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(documents)")
        }
        if "read_at" not in document_columns:
            connection.execute("ALTER TABLE documents ADD COLUMN read_at TEXT")
        document_migrations = {
            "source_path": "TEXT",
            "highlight_status": "TEXT NOT NULL DEFAULT 'idle'",
            "highlight_error": "TEXT",
            "highlight_processed_at": "TEXT",
            "imported_highlight_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in document_migrations.items():
            if column not in document_columns:
                connection.execute(
                    f"ALTER TABLE documents ADD COLUMN {column} {declaration}"
                )
        highlight_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(highlights)")
        }
        if "source" not in highlight_columns:
            connection.execute(
                "ALTER TABLE highlights ADD COLUMN source TEXT NOT NULL DEFAULT 'reader'"
            )
        user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        if "password_hash" in user_columns:
            connection.execute("ALTER TABLE users DROP COLUMN password_hash")
        connection.execute("PRAGMA optimize")
        connection.commit()
    finally:
        connection.close()


def csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def require_csrf(*, header: bool = False) -> None:
    supplied = request.headers.get("X-CSRF-Token", "") if header else request.form.get("csrf_token", "")
    expected = session.get("_csrf", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        abort(400, "Invalid CSRF token")


def normalize_target(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def is_single_word(value: str) -> bool:
    return bool(WORD_RE.fullmatch(value)) and len(value) <= 100


def clean_highlight_rects(value: Any) -> list[dict[str, float]]:
    if not isinstance(value, list) or not value or len(value) > 16:
        raise ValueError("Expected 1-16 rectangles")
    cleaned: list[dict[str, float]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Rectangle must be an object")
        rectangle = {name: round(float(item[name]), 3) for name in ("x1", "y1", "x2", "y2")}
        if not all(math.isfinite(number) and abs(number) <= 100_000 for number in rectangle.values()):
            raise ValueError("Rectangle coordinate is out of range")
        if rectangle["x2"] <= rectangle["x1"] or rectangle["y2"] <= rectangle["y1"]:
            raise ValueError("Rectangle is empty")
        cleaned.append(rectangle)
    return cleaned


def highlight_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "target": row["target"],
        "sentence": row["sentence"],
        "page": row["page"],
        "rects": json.loads(row["rects_json"]),
        "translations": json.loads(row["translations_json"]),
        "replacement": row["replacement"],
        "alternatives": json.loads(row["alternatives_json"]),
        "status": row["status"],
        "error": row["error"],
        "source": row["source"],
    }


class MissingApiKeyError(RuntimeError):
    pass


def enrich_highlight_row(
    app: Flask,
    database: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    user_id: int,
    document_id: str,
) -> sqlite3.Row:
    existing = database.execute(
        """SELECT translations_json, replacement, alternatives_json
           FROM cards WHERE user_id = ? AND target_normalized = ?""",
        (user_id, normalize_target(row["target"])),
    ).fetchone()
    timestamp = now()
    if existing is None:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            raise MissingApiKeyError
        context = make_target_context(
            row["target"],
            row["sentence"],
            context_id=row["id"],
            page=row["page"],
        )
        enriched = enrich_targets(
            [context],
            api_key=api_key,
            model=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
            cache_path=Path(app.config["DATA_DIR"])
            / "highlight_cache"
            / f"{row['id']}.json",
        )[0]
        translations = json.dumps(enriched.translations_ru, ensure_ascii=False)
        replacement = enriched.replacement_ru
        alternatives = json.dumps(
            enriched.forbidden_alternatives_en,
            ensure_ascii=False,
        )
        database.execute(
            """INSERT OR IGNORE INTO cards
               (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                user_id,
                document_id,
                row["target"],
                normalize_target(row["target"]),
                row["sentence"],
                row["page"],
                translations,
                replacement,
                alternatives,
                timestamp,
            ),
        )
    else:
        translations = existing["translations_json"]
        replacement = existing["replacement"]
        alternatives = existing["alternatives_json"]

    database.execute(
        """UPDATE highlights
           SET translations_json = ?, replacement = ?, alternatives_json = ?,
               status = 'ready', error = NULL, updated_at = ?
           WHERE id = ? AND user_id = ?""",
        (translations, replacement, alternatives, timestamp, row["id"], user_id),
    )
    database.commit()
    ready = database.execute(
        "SELECT * FROM highlights WHERE id = ? AND user_id = ?",
        (row["id"], user_id),
    ).fetchone()
    if ready is None:
        raise RuntimeError("Highlight disappeared during enrichment")
    return ready


def write_pdf_without_native_highlights(source: Path, destination: Path) -> None:
    reader = PdfReader(source)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    for page in writer.pages:
        annotations = page.get("/Annots")
        if annotations is None:
            continue
        kept = ArrayObject()
        for reference in annotations:
            annotation = reference.get_object()
            if annotation.get("/Subtype") != "/Highlight":
                kept.append(reference)
        if kept:
            page[NameObject("/Annots")] = kept
        elif NameObject("/Annots") in page:
            del page[NameObject("/Annots")]

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.stem}-",
        suffix=".pdf",
        dir=destination.parent,
        delete=False,
    )
    temporary = Path(temporary_handle.name)
    try:
        with temporary_handle:
            writer.write(temporary_handle)
        PdfReader(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def enqueue_document_processing(app: Flask, document_id: str, user_id: int) -> None:
    jobs: set[str] = app.extensions["highlight_jobs"]
    lock: threading.Lock = app.extensions["highlight_jobs_lock"]
    with lock:
        if document_id in jobs:
            return
        jobs.add(document_id)

    def run() -> None:
        try:
            process_document_highlights(app, document_id, user_id)
        finally:
            with lock:
                jobs.discard(document_id)

    if app.config["PROCESS_DOCUMENTS_INLINE"]:
        run()
    else:
        executor: ThreadPoolExecutor = app.extensions["highlight_executor"]
        executor.submit(run)


def process_document_highlights(app: Flask, document_id: str, user_id: int) -> None:
    database = sqlite3.connect(app.config["DATABASE"], timeout=30)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    try:
        document = database.execute(
            """SELECT * FROM documents
               WHERE id = ? AND user_id = ? AND kind = 'pdf'""",
            (document_id, user_id),
        ).fetchone()
        if document is None:
            return
        database.execute(
            """UPDATE documents
               SET highlight_status = 'processing', highlight_error = NULL
               WHERE id = ? AND user_id = ?""",
            (document_id, user_id),
        )
        database.commit()

        stored_path = Path(document["stored_path"])
        if document["source_path"]:
            source_path = Path(document["source_path"])
        else:
            source_path = stored_path.with_name(f"{document_id}.source.pdf")
            shutil.copy2(stored_path, source_path)
            database.execute(
                "UPDATE documents SET source_path = ? WHERE id = ? AND user_id = ?",
                (str(source_path), document_id, user_id),
            )
            database.commit()

        extracted = extract_highlights(source_path)
        write_pdf_without_native_highlights(source_path, stored_path)
        database.execute(
            "DELETE FROM highlights WHERE document_id = ? AND user_id = ? AND source = 'pdf_import'",
            (document_id, user_id),
        )
        timestamp = now()
        imported_ids: list[str] = []
        for item in extracted:
            rects = clean_highlight_rects(item.rects)
            rects_json = json.dumps(rects, separators=(",", ":"))
            identity = f"{item.context.source_page}:{rects_json}:{normalize_target(item.context.target)}"
            try:
                namespace = uuid.UUID(document_id)
            except ValueError:
                namespace = uuid.NAMESPACE_URL
                identity = f"{document_id}:{identity}"
            highlight_id = str(uuid.uuid5(namespace, identity))
            database.execute(
                """INSERT OR IGNORE INTO highlights
                   (id, user_id, document_id, target, sentence, page, rects_json,
                    translations_json, replacement, alternatives_json, status,
                    error, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '', '[]', 'pending',
                           NULL, 'pdf_import', ?, ?)""",
                (
                    highlight_id,
                    user_id,
                    document_id,
                    item.context.target,
                    item.context.sentence,
                    item.context.source_page,
                    rects_json,
                    timestamp,
                    timestamp,
                ),
            )
            row = database.execute(
                "SELECT id, source FROM highlights WHERE user_id = ? AND document_id = ? AND page = ? AND rects_json = ?",
                (user_id, document_id, item.context.source_page, rects_json),
            ).fetchone()
            if row is not None and row["source"] == "pdf_import":
                imported_ids.append(row["id"])
        database.commit()

        failed = 0
        for highlight_id in imported_ids:
            row = database.execute(
                "SELECT * FROM highlights WHERE id = ? AND user_id = ?",
                (highlight_id, user_id),
            ).fetchone()
            if row is None:
                continue
            try:
                enrich_highlight_row(
                    app,
                    database,
                    row,
                    user_id=user_id,
                    document_id=document_id,
                )
            except MissingApiKeyError:
                failed += 1
                mark_highlight_failed(database, highlight_id, user_id, "OPENROUTER_API_KEY не настроен.")
            except Exception:
                failed += 1
                app.logger.exception("Imported highlight enrichment failed")
                mark_highlight_failed(database, highlight_id, user_id, "Автоперевод временно недоступен.")

        processing_error = (
            f"Не удалось подготовить переводов: {failed}. Можно обработать статью повторно."
            if failed
            else None
        )
        database.execute(
            """UPDATE documents
               SET highlight_status = 'ready', highlight_error = ?,
                   highlight_processed_at = ?, imported_highlight_count = ?
               WHERE id = ? AND user_id = ?""",
            (processing_error, now(), len(imported_ids), document_id, user_id),
        )
        database.commit()
    except Exception:
        app.logger.exception("PDF highlight processing failed")
        database.rollback()
        database.execute(
            """UPDATE documents
               SET highlight_status = 'failed', highlight_error = ?
               WHERE id = ? AND user_id = ?""",
            ("Не удалось обработать хайлайты в PDF.", document_id, user_id),
        )
        database.commit()
    finally:
        database.close()


def mark_highlight_failed(
    database: sqlite3.Connection,
    highlight_id: str,
    user_id: int,
    message: str,
) -> None:
    database.execute(
        """UPDATE highlights SET status = 'failed', error = ?, updated_at = ?
           WHERE id = ? AND user_id = ?""",
        (message, now(), highlight_id, user_id),
    )
    database.commit()


def add_pdf_highlights(source: Path, rows: list[sqlite3.Row]) -> bytes:
    reader = PdfReader(source)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    for row in rows:
        page_index = int(row["page"]) - 1
        if page_index < 0 or page_index >= len(writer.pages):
            continue
        try:
            rectangles = clean_highlight_rects(json.loads(row["rects_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        quad_points = ArrayObject()
        for rectangle in rectangles:
            quad_points.extend(
                FloatObject(value)
                for value in (
                    rectangle["x1"],
                    rectangle["y2"],
                    rectangle["x2"],
                    rectangle["y2"],
                    rectangle["x1"],
                    rectangle["y1"],
                    rectangle["x2"],
                    rectangle["y1"],
                )
            )
        bounds = (
            min(rectangle["x1"] for rectangle in rectangles),
            min(rectangle["y1"] for rectangle in rectangles),
            max(rectangle["x2"] for rectangle in rectangles),
            max(rectangle["y2"] for rectangle in rectangles),
        )
        annotation = Highlight(
            rect=bounds,
            quad_points=quad_points,
            highlight_color="ffe066",
            printing=True,
        )
        translations = json.loads(row["translations_json"])
        note = row["target"]
        if translations:
            note = f"{note}: {', '.join(translations)}"
        annotation[NameObject("/Contents")] = TextStringObject(note)
        writer.add_annotation(page_number=page_index, annotation=annotation)
    stream = io.BytesIO()
    writer.write(stream)
    content = stream.getvalue()
    PdfReader(io.BytesIO(content))
    return content


def make_target_context(
    target: str,
    sentence: str,
    *,
    context_id: str | None = None,
    page: int = 1,
) -> TargetContext:
    match = re.search(re.escape(target), sentence, flags=re.IGNORECASE)
    if match:
        before = html.escape(sentence[: match.start()])
        selected = html.escape(sentence[match.start() : match.end()])
        after = html.escape(sentence[match.end() :])
        sentence_html = f"{before}<b>{selected}</b>{after}"
        recall_html = f"{before}{RECALL_PLACEHOLDER}{after}"
    else:
        sentence_html = html.escape(sentence)
        recall_html = sentence_html
    return TargetContext(
        id=context_id or str(uuid.uuid4()),
        target=target,
        sentence=sentence,
        sentence_html=sentence_html,
        recall_template_html=recall_html,
        source_page=page,
        highlight_coverage=1,
    )


def cards_to_csv(cards: list[sqlite3.Row]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["Front", "Back", "Tags"], quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for card in cards:
        translations = json.loads(card["translations_json"])
        alternatives = json.loads(card["alternatives_json"])
        front = emphasize_target(card["sentence"], card["target"], html.escape(card["target"]))
        back = "<br>".join(f"• {html.escape(value)}" for value in translations)
        tag = re.sub(r"[^A-Za-z0-9_:-]+", "_", Path(card["document_name"]).stem).strip("_") or "article"
        common = f"article::{tag} page::{card['page']}"
        writer.writerow({"Front": front, "Back": back, "Tags": f"{common} card::meaning"})
        replacement = f"<b>{html.escape(card['replacement'])}</b>"
        recall = emphasize_target(card["sentence"], card["target"], replacement, replacement_is_html=True)
        if alternatives:
            recall += f"<br><small>Нельзя использовать: {', '.join(html.escape(value) for value in alternatives)}</small>"
        writer.writerow(
            {"Front": recall, "Back": f"<b>{html.escape(card['target'])}</b>", "Tags": f"{common} card::recall"}
        )
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def emphasize_target(sentence: str, target: str, replacement: str, *, replacement_is_html: bool = False) -> str:
    match = re.search(re.escape(target), sentence, flags=re.IGNORECASE)
    if not match:
        return html.escape(sentence)
    selected = replacement if replacement_is_html else f"<b>{replacement}</b>"
    return f"{html.escape(sentence[:match.start()])}{selected}{html.escape(sentence[match.end():])}"


def safe_download_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "deck"


def now() -> str:
    return datetime.now(UTC).isoformat()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run Anki Papers web application.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    from waitress import serve

    serve(create_app(), host=args.host, port=args.port, threads=4)


if __name__ == "__main__":
    main()
