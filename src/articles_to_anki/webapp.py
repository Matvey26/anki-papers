from __future__ import annotations

import argparse
import csv
import functools
import html
import io
import json
import os
import re
import secrets
import sqlite3
import tempfile
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
from pypdf import PdfReader
from .enrich import DEFAULT_MODEL, enrich_targets, load_env_file
from .extract import RECALL_PLACEHOLDER
from .models import TargetContext

USERNAME_RE = re.compile(r"^[\w.\-]{3,32}$", re.UNICODE)


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    load_env_file(Path.cwd() / ".env")
    app = Flask(__name__)
    data_dir = Path(os.environ.get("ANKI_PAPERS_DATA_DIR", "data")).resolve()
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("APP_SECRET") or secrets.token_hex(32),
        DATA_DIR=data_dir,
        DATABASE=data_dir / "app.sqlite3",
        MAX_CONTENT_LENGTH=85 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    Path(app.config["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    init_database(app)

    app.teardown_appcontext(close_database)
    app.context_processor(lambda: {"csrf_token": csrf_token})

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
            "SELECT * FROM documents WHERE user_id = ? AND kind = 'pdf' ORDER BY created_at DESC",
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
            save_document(upload, "pdf")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
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

    @app.post("/cards")
    @login_required
    def add_card() -> Response:
        require_csrf()
        document_id = request.form.get("document_id", "")
        owned_document(document_id, "pdf")
        target = request.form.get("target", "").strip()
        sentence = request.form.get("sentence", "").strip()
        translations = clean_list(request.form.get("translations", ""), 5)
        replacement = request.form.get("replacement", "").strip()
        alternatives = clean_list(request.form.get("alternatives", ""), 6)
        page_number = max(request.form.get("page", 1, type=int), 1)
        normalized = normalize_target(target)
        if not normalized or not sentence or not translations:
            flash("Нужно слово, предложение и хотя бы один перевод.", "error")
        else:
            try:
                get_database().execute(
                    """INSERT INTO cards
                       (id, user_id, document_id, target, target_normalized, sentence, page,
                        translations_json, replacement, alternatives_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        session["user_id"],
                        document_id,
                        target,
                        normalized,
                        sentence,
                        page_number,
                        json.dumps(translations, ensure_ascii=False),
                        replacement or translations[0],
                        json.dumps(alternatives, ensure_ascii=False),
                        now(),
                    ),
                )
                get_database().commit()
            except sqlite3.IntegrityError:
                flash("Это слово уже было добавлено.", "error")
            else:
                flash("Добавлены две карточки.", "success")
        return redirect(url_for("article", document_id=document_id, page=page_number))

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

    @app.post("/api/enrich")
    @login_required
    def api_enrich() -> Response:
        require_csrf(header=True)
        payload = request.get_json(silent=True) or {}
        target = str(payload.get("target", "")).strip()
        sentence = str(payload.get("sentence", "")).strip()
        if not target or not sentence:
            return jsonify(error="Не хватает слова или предложения."), 400
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            return jsonify(error="OPENROUTER_API_KEY не настроен."), 503
        context = make_target_context(target, sentence)
        try:
            enriched = enrich_targets(
                [context],
                api_key=api_key,
                model=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
                cache_path=Path(app.config["DATA_DIR"]) / "enrichment_cache.json",
            )[0]
        except Exception:
            app.logger.exception("Card enrichment failed")
            return jsonify(error="Автоперевод временно недоступен."), 502
        return jsonify(
            translations=enriched.translations_ru,
            replacement=enriched.replacement_ru,
            alternatives=enriched.forbidden_alternatives_en,
        )

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

    def save_document(upload: Any, kind: str) -> None:
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
        upload.save(stored_path)
        size_limit = 50 * 1024 * 1024 if kind == "pdf" else 80 * 1024 * 1024
        if stored_path.stat().st_size > size_limit:
            stored_path.unlink(missing_ok=True)
            raise ValueError(f"Файл больше {size_limit // 1024 // 1024} МБ.")
        page_count = 0
        text_path: Path | None = None
        try:
            if kind == "pdf":
                reader = PdfReader(stored_path)
                page_count = len(reader.pages)
            get_database().execute(
                """INSERT INTO documents
                   (id, user_id, kind, name, stored_path, text_path, page_count, size, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    document_id,
                    session["user_id"],
                    kind,
                    Path(upload.filename).name,
                    str(stored_path),
                    str(text_path) if text_path else None,
                    page_count,
                    stored_path.stat().st_size,
                    now(),
                ),
            )
            get_database().commit()
        except Exception:
            stored_path.unlink(missing_ok=True)
            if text_path:
                text_path.unlink(missing_ok=True)
            raise

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
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_documents_user_kind
                ON documents(user_id, kind, created_at);
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


def clean_list(value: str, limit: int) -> list[str]:
    result: list[str] = []
    for item in re.split(r"[,;\n]", value):
        cleaned = item.strip()
        if cleaned and cleaned.casefold() not in {existing.casefold() for existing in result}:
            result.append(cleaned)
        if len(result) == limit:
            break
    return result


def make_target_context(target: str, sentence: str) -> TargetContext:
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
        id=str(uuid.uuid4()),
        target=target,
        sentence=sentence,
        sentence_html=sentence_html,
        recall_template_html=recall_html,
        source_page=1,
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
