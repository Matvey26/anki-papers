from __future__ import annotations

import argparse
import csv
import functools
import hashlib
import html
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
from pypdf.errors import PdfReadError
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    FloatObject,
    NameObject,
    TextStringObject,
)
from rapidfuzz import fuzz, process
from werkzeug.middleware.proxy_fix import ProxyFix

from .enrich import (
    DEFAULT_SEMANTIC_MODEL,
    analyse_cluster_assignment,
    approve_context_candidates,
    enrich_targets,  # noqa: F401 - retained as a monkeypatch seam for callers/tests
    load_env_file,
)
from .extract import (
    RECALL_PLACEHOLDER,
    DocumentText,
    DocumentWordIndex,
    document_word_index,
    extract_document_text,
    extract_highlights,
    find_article_contexts,
    find_variant_article_contexts,
)
from .models import (
    ClusterAnalysis,
    ClusterCandidate,
    ClusterExample,
    ContextCandidate,
    TargetContext,
)
from .quick_dictionary import StarDictDictionary
from .security import (
    EncryptedValue,
    claim_token_digest,
    decrypt_value,
    encrypt_value,
    hash_password,
    load_credential_keys,
    password_needs_rehash,
    validate_password,
    verify_password,
)
from .sync_queue import enqueue_sync_job
from .sync_ui import (
    build_sync_status,
    format_sync_time,
    sync_error_text,
    sync_job_reason,
    sync_job_state,
)

USERNAME_RE = re.compile(r"^[\w.\-]{3,32}$", re.UNICODE)
WORD_RE = re.compile(r"^[\w]+(?:['’\-][\w]+)*$", re.UNICODE)
HYPHENATED_LINE_BREAK_RE = re.compile(r"[-\u2010-\u2015\u2212\u00ad]\s*\r?\n\s*")
MAX_TARGET_WORDS = 4
MAX_CLUSTER_CANDIDATES = 5
MAX_CLUSTER_EXAMPLES = 5
CLUSTER_FUZZY_SCORE_CUTOFF = 45.0
ARTICLE_CONTEXT_LIMIT = 4
CONTEXT_CANDIDATES_PER_DOCUMENT = 6
CONTEXT_APPROVAL_CANDIDATE_LIMIT = 14
MAX_CONTEXT_APPROVAL_EXAMPLES = 5
LOGGER = logging.getLogger(__name__)
_ARTICLE_STOPWORDS = {
    "about", "after", "also", "among", "and", "are", "because", "been", "before",
    "being", "between", "both", "but", "can", "could", "does", "during", "each",
    "for", "from", "had", "has", "have", "into", "its", "may", "more", "most",
    "not", "only", "other", "our", "over", "same", "such", "than", "that", "the",
    "their", "these", "they", "this", "through", "under", "using", "was", "were",
    "when", "where", "which", "while", "with", "would",
}


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
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=True,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        ANKIWEB_ALLOWED_USERS=os.environ.get("ANKIWEB_ALLOWED_USERS", "risesduckness"),
        ANKI_CREDENTIAL_KEYS=load_credential_keys(),
    )
    if test_config:
        app.config.update(test_config)
    Path(app.config["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    init_database(app)
    app.extensions["highlight_executor"] = ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="pdf-highlights",
    )
    app.extensions["highlight_jobs"] = set()
    app.extensions["highlight_jobs_lock"] = threading.Lock()
    app.extensions["reader_highlight_jobs"] = set()
    app.extensions["quick_translation_cache"] = {}
    app.extensions["quick_translation_cache_lock"] = threading.Lock()
    app.extensions["quick_translation_dictionary"] = StarDictDictionary.load(
        Path(app.config["DATA_DIR"]) / "dictionaries" / "eng-rus"
    )
    app.extensions["deleted_document_paths"] = {}
    app.extensions["highlight_resume_done"] = False

    app.teardown_appcontext(close_database)
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

    @app.get("/health")
    def health() -> Response:
        return jsonify(ok=True)

    @app.get("/health/worker")
    def worker_health() -> Response:
        row = get_database().execute(
            "SELECT MAX(updated_at) AS updated_at FROM worker_heartbeat"
        ).fetchone()
        if row is None or not row["updated_at"]:
            return jsonify(ok=False), 503
        try:
            updated = datetime.fromisoformat(row["updated_at"])
        except ValueError:
            return jsonify(ok=False), 503
        healthy = datetime.now(UTC) - updated <= timedelta(seconds=90)
        return jsonify(ok=healthy), 200 if healthy else 503

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
            password = request.form.get("password", "")
            ip = request.remote_addr or "unknown"
            database = get_database()
            if login_is_rate_limited(database, username, ip):
                return render_template("auth.html", mode="login"), 429
            user = database.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
            valid = bool(
                USERNAME_RE.fullmatch(username)
                and user is not None
                and verify_password(user["password_hash"], password)
            )
            record_login_attempt(database, username, ip, valid)
            if not valid:
                recent = failed_login_count(database, username, ip)
                time.sleep(min(0.15 * (2 ** max(recent - 1, 0)), 1.2))
                if user is not None and not user["password_hash"]:
                    flash("Для старого профиля нужен одноразовый claim-код.", "error")
                else:
                    flash("Неверный логин или пароль.", "error")
            else:
                if password_needs_rehash(user["password_hash"]):
                    database.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (hash_password(password), user["id"]),
                    )
                    database.commit()
                start_user_session(user)
                return redirect(url_for("dashboard"))
        return render_template("auth.html", mode="login")

    @app.route("/register", methods=["GET", "POST"])
    def register() -> Response:
        if request.method == "POST":
            require_csrf()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirmation = request.form.get("password_confirmation", "")
            try:
                if not USERNAME_RE.fullmatch(username):
                    raise ValueError(
                        "Логин: 3–32 символа; буквы, цифры, точка, дефис или подчёркивание."
                    )
                validate_password(password)
                if password != confirmation:
                    raise ValueError("Пароли не совпадают.")
                database = get_database()
                cursor = database.execute(
                    """INSERT INTO users (username, password_hash, password_set_at, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (username, hash_password(password), now(), now()),
                )
                database.commit()
            except sqlite3.IntegrityError:
                flash("Этот логин уже занят.", "error")
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                user = database.execute(
                    "SELECT id, username FROM users WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                start_user_session(user)
                return redirect(url_for("dashboard"))
        return render_template("auth.html", mode="register")

    @app.route("/claim", methods=["GET", "POST"])
    def claim_account() -> Response:
        if request.method == "POST":
            require_csrf()
            username = request.form.get("username", "").strip()
            code = request.form.get("claim_code", "").strip()
            password = request.form.get("password", "")
            confirmation = request.form.get("password_confirmation", "")
            database = get_database()
            user = database.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
            token = None
            if user is not None and not user["password_hash"]:
                token = database.execute(
                    """SELECT id FROM account_claim_tokens
                       WHERE user_id = ? AND token_hash = ? AND used_at IS NULL
                         AND expires_at > ? ORDER BY created_at DESC LIMIT 1""",
                    (user["id"], claim_token_digest(code), now()),
                ).fetchone()
            try:
                validate_password(password)
                if password != confirmation:
                    raise ValueError("Пароли не совпадают.")
                if token is None:
                    raise ValueError("Claim-код недействителен или истёк.")
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                timestamp = now()
                database.execute(
                    "UPDATE users SET password_hash = ?, password_set_at = ? WHERE id = ?",
                    (hash_password(password), timestamp, user["id"]),
                )
                database.execute(
                    "UPDATE account_claim_tokens SET used_at = ? WHERE id = ?",
                    (timestamp, token["id"]),
                )
                database.commit()
                start_user_session(user)
                return redirect(url_for("dashboard"))
        return render_template("auth.html", mode="claim")

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

    @app.get("/settings")
    @login_required
    def settings() -> Response:
        database = get_database()
        account = database.execute(
            "SELECT * FROM anki_accounts WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        credentials = database.execute(
            "SELECT state FROM user_credentials WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        jobs = database.execute(
            """SELECT state, reason, attempts, run_after, started_at, finished_at,
                      error_code, created_at, updated_at
               FROM sync_jobs WHERE user_id = ?
               ORDER BY created_at DESC LIMIT 5""",
            (session["user_id"],),
        ).fetchall()
        active_job = database.execute(
            """SELECT * FROM sync_jobs
               WHERE user_id = ? AND state IN ('queued', 'running')
               ORDER BY created_at LIMIT 1""",
            (session["user_id"],),
        ).fetchone()
        pending_words = database.execute(
            "SELECT COUNT(*) FROM cards WHERE user_id = ? AND anki_synced_at IS NULL",
            (session["user_id"],),
        ).fetchone()[0]
        allowed = ankiweb_enabled_for_user(current_app, session["username"])
        return render_template(
            "settings.html",
            account=account,
            credentials=credentials,
            jobs=jobs,
            sync_status=build_sync_status(
                account, active_job, jobs[0] if jobs else None, pending_words
            ),
            decks=json.loads(account["available_decks_json"]) if account else [],
            ankiweb_allowed=allowed,
            credentials_configured=bool(current_app.config["ANKI_CREDENTIAL_KEYS"]),
        )

    @app.post("/settings/password")
    @login_required
    def change_password() -> Response:
        require_csrf()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirmation = request.form.get("password_confirmation", "")
        database = get_database()
        user = database.execute(
            "SELECT password_hash FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
        try:
            if user is None or not verify_password(user["password_hash"], current_password):
                raise ValueError("Текущий пароль неверен.")
            validate_password(new_password)
            if new_password != confirmation:
                raise ValueError("Пароли не совпадают.")
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            database.execute(
                "UPDATE users SET password_hash = ?, password_set_at = ? WHERE id = ?",
                (hash_password(new_password), now(), session["user_id"]),
            )
            database.commit()
            session.clear()
            flash("Пароль изменён. Войдите снова.", "success")
            return redirect(url_for("login"))
        return redirect(url_for("settings"))

    @app.post("/settings/anki/connect")
    @login_required
    def connect_ankiweb() -> Response:
        require_csrf()
        if not ankiweb_enabled_for_user(current_app, session["username"]):
            abort(403)
        database = get_database()
        site_password = request.form.get("site_password", "")
        ankiweb_id = request.form.get("ankiweb_id", "").strip()
        ankiweb_password = request.form.get("ankiweb_password", "")
        user = database.execute(
            "SELECT password_hash FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
        keys = current_app.config["ANKI_CREDENTIAL_KEYS"]
        if not keys:
            flash("Серверный ключ AnkiWeb не настроен.", "error")
            return redirect(url_for("settings"))
        if user is None or not verify_password(user["password_hash"], site_password):
            flash("Пароль сайта неверен.", "error")
            return redirect(url_for("settings"))
        if not ankiweb_id or not ankiweb_password or len(ankiweb_password) > 1024:
            flash("Введите AnkiWeb ID и пароль.", "error")
            return redirect(url_for("settings"))
        encrypted_id = encrypt_value(
            ankiweb_id,
            user_id=session["user_id"],
            field="ankiweb_id",
            keys=keys,
        )
        encrypted_password = encrypt_value(
            ankiweb_password,
            user_id=session["user_id"],
            field="ankiweb_password",
            keys=keys,
        )
        timestamp = now()
        database.execute(
            """INSERT INTO user_credentials
               (user_id, ankiweb_id_ciphertext, ankiweb_id_nonce,
                password_ciphertext, password_nonce, hkey_ciphertext, hkey_nonce,
                key_version, state, auth_failures, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'pending', 0, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 ankiweb_id_ciphertext = excluded.ankiweb_id_ciphertext,
                 ankiweb_id_nonce = excluded.ankiweb_id_nonce,
                 password_ciphertext = excluded.password_ciphertext,
                 password_nonce = excluded.password_nonce,
                 hkey_ciphertext = NULL, hkey_nonce = NULL,
                 key_version = excluded.key_version, state = 'pending',
                 auth_failures = 0, updated_at = excluded.updated_at""",
            (
                session["user_id"],
                encrypted_id.ciphertext,
                encrypted_id.nonce,
                encrypted_password.ciphertext,
                encrypted_password.nonce,
                encrypted_id.key_version,
                timestamp,
                timestamp,
            ),
        )
        database.execute(
            """INSERT INTO anki_accounts (user_id, state, updated_at)
               VALUES (?, 'connecting', ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 state = 'connecting', last_error = NULL,
                 available_decks_json = '[]', selected_deck_id = NULL,
                 selected_deck_name = NULL, updated_at = excluded.updated_at""",
            (session["user_id"], timestamp),
        )
        enqueue_sync_job(database, session["user_id"], "connect", delay_seconds=0)
        database.commit()
        flash("Проверка AnkiWeb и полное скачивание поставлены в очередь.", "success")
        return redirect(url_for("settings"))

    @app.post("/settings/anki/deck")
    @login_required
    def select_anki_deck() -> Response:
        require_csrf()
        database = get_database()
        account = database.execute(
            "SELECT * FROM anki_accounts WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        try:
            deck_id = int(request.form.get("deck_id", ""))
            decks = json.loads(account["available_decks_json"]) if account else []
            selected = next(deck for deck in decks if int(deck["id"]) == deck_id)
        except (StopIteration, TypeError, ValueError, json.JSONDecodeError):
            abort(400, "Некорректная колода")
        timestamp = now()
        database.execute(
            """UPDATE anki_accounts
               SET selected_deck_id = ?, selected_deck_name = ?, state = 'connected',
                   last_error = NULL, updated_at = ? WHERE user_id = ?""",
            (deck_id, selected["name"], timestamp, session["user_id"]),
        )
        enqueue_sync_job(database, session["user_id"], "initial_sync", delay_seconds=0)
        database.commit()
        flash("Колода выбрана. Карточки синхронизируются в фоне.", "success")
        return redirect(url_for("settings"))

    @app.post("/settings/anki/sync")
    @login_required
    def sync_ankiweb_now() -> Response:
        require_csrf()
        database = get_database()
        account = database.execute(
            "SELECT state FROM anki_accounts WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        if account is None or account["state"] not in {"connected", "error"}:
            flash("AnkiWeb ещё не готов к синхронизации.", "error")
        else:
            enqueue_sync_job(database, session["user_id"], "manual", delay_seconds=0)
            database.commit()
            flash("Синхронизация поставлена в очередь.", "success")
        destination = request.form.get("next")
        if destination not in {url_for("dashboard"), url_for("settings")}:
            destination = url_for("settings")
        return redirect(destination)

    @app.post("/settings/anki/disconnect")
    @login_required
    def disconnect_ankiweb() -> Response:
        require_csrf()
        database = get_database()
        account = database.execute(
            "SELECT mirror_path FROM anki_accounts WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        database.execute(
            """UPDATE sync_jobs SET state = 'cancelled', finished_at = ?, updated_at = ?
               WHERE user_id = ? AND state IN ('queued', 'running')""",
            (now(), now(), session["user_id"]),
        )
        database.execute("DELETE FROM anki_note_links WHERE user_id = ?", (session["user_id"],))
        database.execute("DELETE FROM user_credentials WHERE user_id = ?", (session["user_id"],))
        database.execute("DELETE FROM anki_accounts WHERE user_id = ?", (session["user_id"],))
        database.commit()
        if account and account["mirror_path"]:
            remove_managed_files(Path(current_app.config["DATA_DIR"]), [account["mirror_path"]])
        flash("AnkiWeb отключён; секреты, зеркало и ожидающие задания удалены.", "success")
        return redirect(url_for("settings"))

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
        from .apkg import merge

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
        from .rebuild import build_rebuilt_deck_apkg

        old_deck = request.files.get("old_deck")
        with tempfile.TemporaryDirectory(prefix="anki-papers-rebuild-") as temporary_name:
            temporary = Path(temporary_name)
            source_path: Path | None = None
            if old_deck is not None and old_deck.filename:
                head = old_deck.stream.read(5)
                old_deck.stream.seek(0)
                if not head.startswith(b"PK"):
                    flash("Старая колода должна быть файлом APKG.", "error")
                    return redirect(url_for("settings"))
                source_path = temporary / "old-deck.apkg"
                old_deck.save(source_path)
            database = get_database()
            user_id = session["user_id"]
            try:
                if source_path is None:
                    mirror = decrypt_mirror_collection(database, user_id)
                    if mirror is not None:
                        mirror_path = temporary / "mirror-collection.anki2"
                        mirror_path.write_bytes(mirror)
                        source_path = mirror_path
                content = build_rebuilt_deck_apkg(
                    database,
                    user_id,
                    data_dir=Path(app.config["DATA_DIR"]),
                    source_path=source_path,
                )
            except Exception:
                app.logger.exception("Deck rebuild failed")
                flash(
                    "Не удалось пересобрать колоду. Попробуйте загрузить файл APKG "
                    "старой колоды вручную.",
                    "error",
                )
                return redirect(url_for("settings"))
        return Response(
            content,
            mimetype="application/octet-stream",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="anki-papers-rebuild-{datetime.now(UTC).date()}.apkg"'
                )
            },
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

    def reconcile_apkg_exports(deck: sqlite3.Row) -> None:
        from .apkg import card_identities, managed_identities

        deck_identities = managed_identities(Path(deck["stored_path"]))
        cards = get_database().execute(
            """SELECT cards.*, documents.name AS document_name
               FROM cards JOIN documents ON documents.id = cards.document_id
               WHERE cards.user_id = ? ORDER BY cards.created_at""",
            (session["user_id"],),
        ).fetchall()
        timestamp = now()
        updates = []
        for card in cards:
            rows = csv.DictReader(
                io.StringIO(cards_to_csv([card]).decode("utf-8-sig"))
            )
            complete = all(
                bool(card_identities(row["Front"], row["Back"], row["Tags"]) & deck_identities)
                for row in rows
            )
            exported_at = (card["apkg_exported_at"] or timestamp) if complete else None
            updates.append((exported_at, card["id"], session["user_id"]))
        get_database().executemany(
            "UPDATE cards SET apkg_exported_at = ? WHERE id = ? AND user_id = ?",
            updates,
        )
        get_database().commit()

    return app


def login_required(view: Callable[..., Response]) -> Callable[..., Response]:
    @functools.wraps(view)
    def wrapped(**kwargs: Any) -> Response:
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = get_database().execute(
            "SELECT username, password_hash FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()
        if user is None:
            session.clear()
            return redirect(url_for("login"))
        if not user["password_hash"]:
            session.clear()
            flash("Для старого профиля нужен одноразовый claim-код.", "error")
            return redirect(url_for("claim_account"))
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


def start_user_session(user: sqlite3.Row) -> None:
    session.clear()
    session.permanent = True
    session["session_id"] = secrets.token_urlsafe(24)
    session["user_id"] = user["id"]
    session["username"] = user["username"]


def failed_login_count(
    database: sqlite3.Connection, username: str, ip_address: str
) -> int:
    cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    return int(
        database.execute(
            """SELECT COUNT(*) FROM login_attempts
               WHERE successful = 0 AND created_at >= ?
                 AND (username = ? COLLATE NOCASE OR ip_address = ?)""",
            (cutoff, username, ip_address),
        ).fetchone()[0]
    )


def login_is_rate_limited(
    database: sqlite3.Connection, username: str, ip_address: str
) -> bool:
    return failed_login_count(database, username, ip_address) >= 8


def record_login_attempt(
    database: sqlite3.Connection,
    username: str,
    ip_address: str,
    successful: bool,
) -> None:
    if successful:
        database.execute(
            """DELETE FROM login_attempts
               WHERE successful = 0
                 AND (username = ? COLLATE NOCASE OR ip_address = ?)""",
            (username, ip_address),
        )
    database.execute(
        """INSERT INTO login_attempts (username, ip_address, successful, created_at)
           VALUES (?, ?, ?, ?)""",
        (username[:64], ip_address[:64], int(successful), now()),
    )
    database.execute(
        "DELETE FROM login_attempts WHERE created_at < ?",
        ((datetime.now(UTC) - timedelta(days=2)).isoformat(),),
    )
    database.commit()


def ankiweb_enabled_for_user(app: Flask, username: str) -> bool:
    configured = str(app.config.get("ANKIWEB_ALLOWED_USERS", "")).strip()
    if configured == "*":
        return True
    allowed = {value.strip().casefold() for value in configured.split(",") if value.strip()}
    return username.casefold() in allowed


def init_database(app: Flask) -> None:
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT,
                password_set_at TEXT,
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
                last_page INTEGER,
                last_opened_at TEXT,
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
                lemma TEXT,
                family_key TEXT,
                part_of_speech TEXT,
                sense_definition_en TEXT,
                contexts_json TEXT,
                semantic_version INTEGER NOT NULL DEFAULT 0,
                csv_exported_at TEXT,
                apkg_exported_at TEXT,
                anki_synced_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, document_id, page, target_normalized, sentence)
            );
            CREATE INDEX IF NOT EXISTS idx_cards_user_created
                ON cards(user_id, created_at);
            CREATE TABLE IF NOT EXISTS card_highlights (
                card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                highlight_id TEXT NOT NULL REFERENCES highlights(id) ON DELETE CASCADE,
                PRIMARY KEY(card_id, highlight_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_card_highlights_highlight
                ON card_highlights(highlight_id);
            CREATE TABLE IF NOT EXISTS deleted_highlights (
                highlight_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_deleted_highlights_document
                ON deleted_highlights(user_id, document_id);
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE,
                ip_address TEXT NOT NULL,
                successful INTEGER NOT NULL CHECK(successful IN (0, 1)),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup
                ON login_attempts(username, ip_address, created_at);
            CREATE TABLE IF NOT EXISTS account_claim_tokens (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_claim_tokens_user
                ON account_claim_tokens(user_id, expires_at);
            CREATE TABLE IF NOT EXISTS user_credentials (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                ankiweb_id_ciphertext BLOB NOT NULL,
                ankiweb_id_nonce BLOB NOT NULL,
                password_ciphertext BLOB NOT NULL,
                password_nonce BLOB NOT NULL,
                hkey_ciphertext BLOB,
                hkey_nonce BLOB,
                key_version INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK(state IN ('pending', 'active', 'needs_reconnect')),
                auth_failures INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS anki_accounts (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                selected_deck_id INTEGER,
                selected_deck_name TEXT,
                available_decks_json TEXT NOT NULL DEFAULT '[]',
                mirror_path TEXT,
                mirror_nonce BLOB,
                mirror_key_version INTEGER,
                state TEXT NOT NULL DEFAULT 'connecting'
                    CHECK(state IN ('connecting', 'awaiting_deck', 'connected', 'syncing', 'needs_reconnect', 'error')),
                last_success_at TEXT,
                last_error TEXT,
                last_added_count INTEGER NOT NULL DEFAULT 0,
                preview_existing INTEGER NOT NULL DEFAULT 0,
                preview_missing INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS anki_note_links (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                site_card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                direction TEXT NOT NULL CHECK(direction IN ('meaning', 'recall')),
                note_id INTEGER NOT NULL,
                note_guid TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, site_card_id, direction),
                UNIQUE(user_id, note_id)
            );
            CREATE TABLE IF NOT EXISTS sync_jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reason TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued'
                    CHECK(state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
                attempts INTEGER NOT NULL DEFAULT 0,
                run_after TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_jobs_one_queued_user
                ON sync_jobs(user_id) WHERE state = 'queued';
            CREATE INDEX IF NOT EXISTS idx_sync_jobs_ready
                ON sync_jobs(state, run_after, created_at);
            CREATE TABLE IF NOT EXISTS worker_heartbeat (
                worker_name TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL
            );
            """
        )
        document_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(documents)")
        }
        if "read_at" not in document_columns:
            connection.execute("ALTER TABLE documents ADD COLUMN read_at TEXT")
        document_migrations = {
            "text_path": "TEXT",
            "last_page": "INTEGER",
            "last_opened_at": "TEXT",
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
        if "password_hash" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        if "password_set_at" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN password_set_at TEXT")
        card_columns = {row[1] for row in connection.execute("PRAGMA table_info(cards)")}
        if "anki_synced_at" not in card_columns:
            connection.execute("ALTER TABLE cards ADD COLUMN anki_synced_at TEXT")
        semantic_columns = {
            "lemma": "TEXT",
            "family_key": "TEXT",
            "part_of_speech": "TEXT",
            "sense_definition_en": "TEXT",
            "contexts_json": "TEXT",
            "semantic_version": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in semantic_columns.items():
            if column not in card_columns:
                connection.execute(f"ALTER TABLE cards ADD COLUMN {column} {declaration}")
        connection.execute("DROP INDEX IF EXISTS idx_cards_semantic_family_lookup")
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_cards_cluster_leader
               ON cards(user_id, semantic_version, target_normalized)"""
        )
        account_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(anki_accounts)")
        }
        if "preview_existing" not in account_columns:
            connection.execute(
                "ALTER TABLE anki_accounts ADD COLUMN preview_existing INTEGER NOT NULL DEFAULT 0"
            )
        if "preview_missing" not in account_columns:
            connection.execute(
                "ALTER TABLE anki_accounts ADD COLUMN preview_missing INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """INSERT OR IGNORE INTO deleted_highlights
               (highlight_id, user_id, document_id, created_at)
               SELECT id, user_id, document_id, ? FROM highlights
               WHERE status = 'failed'""",
            (now(),),
        )
        connection.execute("DELETE FROM highlights WHERE status = 'failed'")
        migrate_cards_to_contexts(connection)
        card_columns = {row[1] for row in connection.execute("PRAGMA table_info(cards)")}
        for column, declaration in semantic_columns.items():
            if column not in card_columns:
                connection.execute(f"ALTER TABLE cards ADD COLUMN {column} {declaration}")
        connection.execute("DROP INDEX IF EXISTS idx_cards_semantic_family_lookup")
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_cards_cluster_leader
               ON cards(user_id, semantic_version, target_normalized)"""
        )
        purge_llm_generated_contexts(connection)
        synchronize_card_highlights_by_context(connection)
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
    return normalize_selected_text(value)


def card_context_key(row: sqlite3.Row) -> tuple[int, str, int, str, str]:
    return (
        int(row["user_id"]),
        str(row["document_id"]),
        int(row["page"]),
        normalize_target(row["target"]),
        " ".join(str(row["sentence"]).casefold().split()),
    )


def purge_llm_generated_contexts(connection: sqlite3.Connection) -> None:
    """Drop synthetic LLM contexts from legacy cards on startup."""
    rows = connection.execute(
        "SELECT id, contexts_json FROM cards WHERE semantic_version = 1"
    ).fetchall()
    for row in rows:
        contexts = json.loads(row["contexts_json"] or "[]")
        retained = [
            item
            for item in contexts
            if not (
                isinstance(item, dict)
                and item.get("source") == "llm_generated"
            )
        ]
        if len(retained) == len(contexts):
            continue
        connection.execute(
            "UPDATE cards SET contexts_json = ? WHERE id = ?",
            (json.dumps(retained, ensure_ascii=False), row["id"]),
        )


def migrate_cards_to_contexts(connection: sqlite3.Connection) -> None:
    schema_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cards'"
    ).fetchone()
    schema = "" if schema_row is None else "".join(str(schema_row[0]).split())
    if "UNIQUE(user_id,target_normalized)" not in schema:
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript(
        """
        DROP TABLE IF EXISTS anki_note_links;
        ALTER TABLE card_highlights RENAME TO card_highlights_by_target;
        ALTER TABLE cards RENAME TO cards_by_target;
        CREATE TABLE cards (
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
            anki_synced_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, document_id, page, target_normalized, sentence)
        );
        INSERT INTO cards
            (id, user_id, document_id, target, target_normalized, sentence, page,
             translations_json, replacement, alternatives_json, csv_exported_at,
             apkg_exported_at, anki_synced_at, created_at)
        SELECT id, user_id, document_id, target, target_normalized, sentence, page,
               translations_json, replacement, alternatives_json, csv_exported_at,
               apkg_exported_at, anki_synced_at, created_at
        FROM cards_by_target;
        DROP TABLE card_highlights_by_target;
        DROP TABLE cards_by_target;
        CREATE INDEX idx_cards_user_created ON cards(user_id, created_at);
        CREATE TABLE card_highlights (
            card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            highlight_id TEXT NOT NULL REFERENCES highlights(id) ON DELETE CASCADE,
            PRIMARY KEY(card_id, highlight_id)
        );
        CREATE UNIQUE INDEX idx_card_highlights_highlight
            ON card_highlights(highlight_id);
        CREATE TABLE anki_note_links (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            site_card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            direction TEXT NOT NULL CHECK(direction IN ('meaning', 'recall')),
            note_id INTEGER NOT NULL,
            note_guid TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id, site_card_id, direction),
            UNIQUE(user_id, note_id)
        );
        """
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")


def synchronize_card_highlights_by_context(connection: sqlite3.Connection) -> None:
    cards = connection.execute("SELECT * FROM cards").fetchall()
    semantic_cards_by_highlight = {
        str(row["highlight_id"]): row
        for row in connection.execute(
            """SELECT card_highlights.highlight_id, cards.*
               FROM card_highlights
               JOIN cards ON cards.id = card_highlights.card_id
               WHERE cards.semantic_version = 1"""
        ).fetchall()
    }
    cards_by_context = {card_context_key(row): row for row in cards}
    cards_by_target = {
        (int(row["user_id"]), normalize_target(row["target"])): row
        for row in cards
    }
    connection.execute("DELETE FROM card_highlights")
    highlights = connection.execute(
        "SELECT * FROM highlights WHERE status = 'ready' ORDER BY created_at"
    ).fetchall()
    for highlight in highlights:
        key = card_context_key(highlight)
        card = semantic_cards_by_highlight.get(str(highlight["id"]))
        if card is None:
            card = cards_by_context.get(key)
        if card is None:
            template = cards_by_target.get((key[0], key[3]))
            card_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO cards
                   (id, user_id, document_id, target, target_normalized, sentence,
                    page, translations_json, replacement, alternatives_json,
                    csv_exported_at, apkg_exported_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
                (
                    card_id,
                    highlight["user_id"],
                    highlight["document_id"],
                    highlight["target"],
                    key[3],
                    highlight["sentence"],
                    highlight["page"],
                    highlight["translations_json"] if template is None else template["translations_json"],
                    highlight["replacement"] if template is None else template["replacement"],
                    highlight["alternatives_json"] if template is None else template["alternatives_json"],
                    highlight["created_at"],
                ),
            )
            card = connection.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
            if card is None:
                raise RuntimeError("Failed to split card contexts")
            cards_by_context[key] = card
            cards_by_target[(key[0], key[3])] = card
        connection.execute(
            "INSERT INTO card_highlights (card_id, highlight_id) VALUES (?, ?)",
            (card["id"], highlight["id"]),
        )


def word_count_label(value: int) -> str:
    value = int(value)
    remainder = value % 100
    if 11 <= remainder <= 14:
        noun = "слов"
    elif value % 10 == 1:
        noun = "слово"
    elif 2 <= value % 10 <= 4:
        noun = "слова"
    else:
        noun = "слов"
    return f"{value} {noun}"


def normalize_selected_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value))
    value = HYPHENATED_LINE_BREAK_RE.sub("", value)
    value = value.replace("\u00ad", "")
    cleaned: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if character in {"'", "’", "‘", "ʼ", "＇"}:
            cleaned.append("'")
        elif category == "Pd" or character == "\u2212":
            cleaned.append("-")
        elif category[0] in {"L", "M", "N"}:
            cleaned.append(character.casefold())
        elif character.isspace():
            cleaned.append(" ")
        else:
            cleaned.append(" ")
    normalized = " ".join("".join(cleaned).split())
    normalized = re.sub(r"-{2,}", "-", normalized)
    normalized = re.sub(r"'{2,}", "'", normalized)
    return normalized.strip(" -'")


def is_selectable_target(value: str) -> bool:
    words = value.split()
    return (
        1 <= len(words) <= MAX_TARGET_WORDS
        and all(WORD_RE.fullmatch(word) for word in words)
        and len(value) <= 100
    )


def quick_translation_groups(word: str) -> list[dict[str, Any]]:
    dictionary = current_app.extensions.get("quick_translation_dictionary")
    if isinstance(dictionary, StarDictDictionary):
        return dictionary.lookup(word)
    return []


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


def highlight_rects_match(
    first: list[dict[str, float]],
    second: list[dict[str, float]],
    *,
    minimum_overlap: float = 0.60,
) -> bool:
    first_area = sum(
        (rectangle["x2"] - rectangle["x1"])
        * (rectangle["y2"] - rectangle["y1"])
        for rectangle in first
    )
    second_area = sum(
        (rectangle["x2"] - rectangle["x1"])
        * (rectangle["y2"] - rectangle["y1"])
        for rectangle in second
    )
    smaller_area = min(first_area, second_area)
    if smaller_area <= 0:
        return False
    intersection = 0.0
    for left in first:
        for right in second:
            width = max(0.0, min(left["x2"], right["x2"]) - max(left["x1"], right["x1"]))
            height = max(0.0, min(left["y2"], right["y2"]) - max(left["y1"], right["y1"]))
            intersection += width * height
    return intersection / smaller_area >= minimum_overlap


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


def delete_highlight_rows(
    database: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    user_id: int,
) -> None:
    if not rows:
        return
    card_ids: set[str] = set()
    removed_context_ids: dict[str, set[str]] = {}
    timestamp = now()
    for row in rows:
        linked = database.execute(
            "SELECT card_id FROM card_highlights WHERE highlight_id = ?",
            (row["id"],),
        ).fetchall()
        card_ids.update(link["card_id"] for link in linked)
        for link in linked:
            removed_context_ids.setdefault(str(link["card_id"]), set()).add(str(row["id"]))
        database.execute(
            """INSERT OR IGNORE INTO deleted_highlights
               (highlight_id, user_id, document_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (row["id"], user_id, row["document_id"], timestamp),
        )
        database.execute(
            "DELETE FROM highlights WHERE id = ? AND user_id = ?",
            (row["id"], user_id),
        )

    for card_id in card_ids:
        card = database.execute(
            "SELECT * FROM cards WHERE id = ? AND user_id = ?", (card_id, user_id)
        ).fetchone()
        if card is None:
            continue
        replacement = database.execute(
            """SELECT highlights.* FROM highlights
               JOIN card_highlights ON card_highlights.highlight_id = highlights.id
               WHERE card_highlights.card_id = ? AND highlights.user_id = ?
               ORDER BY highlights.created_at LIMIT 1""",
            (card_id, user_id),
        ).fetchone()
        if replacement is None:
            database.execute(
                "DELETE FROM cards WHERE id = ? AND user_id = ?",
                (card_id, user_id),
            )
        elif card["semantic_version"] == 1:
            removed = removed_context_ids.get(card_id, set())
            contexts = json.loads(card["contexts_json"] or "[]")
            contexts = [
                item for item in contexts
                if str(item.get("id")) not in removed
            ]
            database.execute(
                """UPDATE cards SET document_id = ?, target = ?, target_normalized = ?,
                   sentence = ?, page = ?, contexts_json = ?, csv_exported_at = NULL,
                   apkg_exported_at = NULL, anki_synced_at = NULL
                   WHERE id = ? AND user_id = ?""",
                (
                    replacement["document_id"], card["target"], card["target_normalized"],
                    replacement["sentence"], replacement["page"],
                    json.dumps(contexts, ensure_ascii=False), card_id, user_id,
                ),
            )
        else:
            database.execute(
                """UPDATE cards
                   SET document_id = ?, target = ?, target_normalized = ?,
                       sentence = ?, page = ?
                   WHERE id = ? AND user_id = ?""",
                (
                    replacement["document_id"],
                    replacement["target"],
                    normalize_target(replacement["target"]),
                    replacement["sentence"],
                    replacement["page"],
                    card_id,
                    user_id,
                ),
            )
    database.commit()


class MissingApiKeyError(RuntimeError):
    pass


def cluster_examples(
    database: sqlite3.Connection,
    card: sqlite3.Row,
) -> list[ClusterExample]:
    rows = database.execute(
        """SELECT highlights.target, highlights.sentence
           FROM highlights
           JOIN card_highlights ON card_highlights.highlight_id = highlights.id
           WHERE card_highlights.card_id = ? AND highlights.user_id = ?
           ORDER BY highlights.created_at""",
        (card["id"], card["user_id"]),
    ).fetchall()
    values: list[ClusterExample] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        highlight = normalize_selected_text(str(row["target"]))
        context = " ".join(str(row["sentence"]).split())
        key = (highlight, context.casefold())
        if not highlight or not context or key in seen:
            continue
        seen.add(key)
        values.append(ClusterExample(highlight=highlight, context=context))
        if len(values) == MAX_CLUSTER_EXAMPLES:
            return values

    for item in json.loads(card["contexts_json"] or "[]"):
        if not isinstance(item, dict) or item.get("source") != "user_pdf":
            continue
        highlight = normalize_selected_text(str(item.get("target") or ""))
        context = " ".join(str(item.get("sentence") or "").split())
        key = (highlight, context.casefold())
        if not highlight or not context or key in seen:
            continue
        seen.add(key)
        values.append(ClusterExample(highlight=highlight, context=context))
        if len(values) == MAX_CLUSTER_EXAMPLES:
            return values

    if not values:
        values.append(
            ClusterExample(
                highlight=normalize_selected_text(str(card["target"])),
                context=" ".join(str(card["sentence"]).split()),
            )
        )
    return values


def find_cluster_candidates(
    database: sqlite3.Connection,
    user_id: int,
    normalized_highlight: str,
    *,
    limit: int = MAX_CLUSTER_CANDIDATES,
) -> list[ClusterCandidate]:
    cards = database.execute(
        """SELECT * FROM cards
           WHERE user_id = ? AND semantic_version = 1
           ORDER BY created_at, id""",
        (user_id,),
    ).fetchall()
    leaders = {
        str(card["id"]): normalize_selected_text(
            str(card["target"] or card["family_key"] or card["lemma"] or "")
        )
        for card in cards
    }
    leaders = {cluster_id: leader for cluster_id, leader in leaders.items() if leader}
    matches = process.extract(
        normalized_highlight,
        leaders,
        scorer=fuzz.WRatio,
        processor=None,
        score_cutoff=CLUSTER_FUZZY_SCORE_CUTOFF,
        limit=max(1, min(limit, MAX_CLUSTER_CANDIDATES)),
    )
    cards_by_id = {str(card["id"]): card for card in cards}
    return [
        ClusterCandidate(
            cluster_id=str(cluster_id),
            leader=leader,
            examples=cluster_examples(database, cards_by_id[str(cluster_id)]),
        )
        for leader, _score, cluster_id in matches
    ]


def merge_semantic_translations(*groups: list[str], limit: int = 8) -> list[str]:
    merged: list[str] = []
    normalized: set[str] = set()
    for group in groups:
        for value in group:
            cleaned = value.strip()
            key = cleaned.casefold()
            if cleaned and key not in normalized:
                merged.append(cleaned)
                normalized.add(key)
            if len(merged) == limit:
                return merged
    return merged


def _article_words(value: str, *, targets: set[str]) -> set[str]:
    target_words = {
        token
        for target in targets
        for token in re.findall(r"[a-z]+", target.casefold())
    }
    return {
        token
        for token in re.findall(r"[a-z]+", value.casefold())
        if len(token) >= 3
        and token not in _ARTICLE_STOPWORDS
        and token not in target_words
    }


def _article_context_score(
    card: sqlite3.Row,
    contexts: list[dict[str, Any]],
    target: str,
    sentence: str,
    targets: set[str],
) -> int | None:
    if not _article_context_is_readable(sentence):
        return None
    target_word_count = len(re.findall(r"[A-Za-z]+", target))
    anchor_text = " ".join(
        [str(card["sense_definition_en"])]
        + [
            str(item.get("sentence") or "")
            for item in contexts
            if item.get("source") not in {"article_context", "llm_generated"}
        ]
    )
    anchor_words = _article_words(anchor_text, targets=targets)
    candidate_words = _article_words(sentence, targets=targets)
    overlap = len(anchor_words & candidate_words)
    if target_word_count >= 2:
        return 100 + overlap
    if overlap < 2:
        return None
    return overlap


def _article_context_is_readable(sentence: str) -> bool:
    cleaned = sentence.strip()
    if len(cleaned) < 45 or cleaned.endswith(" .") or "|" in cleaned:
        return False
    if re.match(r"^(?:…\s*)?\d+\s+\d+(?:\.\d+)+\s+", cleaned):
        return False
    numeric_tokens = re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?%?", cleaned)
    percentages = [value for value in numeric_tokens if value.endswith("%")]
    if len(numeric_tokens) >= 8 or len(percentages) >= 3:
        return False
    if cleaned.startswith("…") and len(numeric_tokens) >= 4:
        return False
    if sum(unicodedata.category(char).startswith("S") for char in cleaned) >= 5:
        return False
    return True


def _llm_cache_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "caches"


def _read_cache_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_cache_file(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _candidate_cache_payload(candidate: Any) -> dict[str, Any]:
    """Stable content of one cluster candidate for cache keys (model or dict)."""
    cluster_id = getattr(candidate, "cluster_id", None)
    leader = getattr(candidate, "leader", None)
    examples = getattr(candidate, "examples", None)
    if cluster_id is None and isinstance(candidate, dict):
        cluster_id = candidate.get("cluster_id")
        leader = candidate.get("leader")
        examples = candidate.get("examples")
    example_payload = []
    for item in examples or []:
        highlight = getattr(item, "highlight", None)
        context = getattr(item, "context", None)
        if highlight is None and isinstance(item, dict):
            highlight = item.get("highlight")
            context = item.get("context")
        example_payload.append(
            {"highlight": str(highlight or ""), "context": str(context or "")}
        )
    return {"cluster_id": str(cluster_id or ""), "leader": str(leader or ""), "examples": example_payload}


def cluster_analysis_cached(
    cache_dir: Path,
    user_id: int,
    *,
    model: str,
    target: str,
    normalized_target: str,
    sentence: str,
    candidates: list[Any],
    api_key: str | None,
) -> tuple[ClusterAnalysis, bool]:
    """Analyse one highlight into a semantic cluster, reusing cached results.

    The cache is keyed by the exact model inputs, so repeated enrichments and
    deck rebuilds never pay for the same LLM call twice. Returns the analysis
    and whether it came from the cache.
    """
    if any(isinstance(candidate, dict) for candidate in candidates):
        candidates = [
            ClusterCandidate.model_validate(candidate) for candidate in candidates
        ]
    key_payload = json.dumps(
        {
            "model": model,
            "target": target,
            "normalized_target": normalized_target,
            "sentence": sentence,
            "candidates": [
                _candidate_cache_payload(candidate) for candidate in candidates
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
    path = cache_dir / f"cluster-analysis-{user_id}.json"
    cache = _read_cache_file(path)
    cached = cache.get(key)
    if cached is not None:
        try:
            return ClusterAnalysis.model_validate(cached), True
        except Exception:
            pass
    if not api_key:
        raise MissingApiKeyError
    analysis = analyse_cluster_assignment(
        target,
        normalized_target,
        sentence,
        candidates,
        api_key=api_key,
        model=model,
    )
    cache = _read_cache_file(path)
    cache[key] = analysis.model_dump()
    _write_cache_file(path, cache)
    return analysis, False


def context_approval_cached(
    cache_dir: Path,
    user_id: int,
    *,
    model: str,
    leader: str,
    definition: str,
    translations: list[str],
    known_contexts: list[str],
    candidates: list[ContextCandidate],
    api_key: str | None,
) -> set[str] | None:
    """Approve mined article contexts, reusing cached decisions by content."""
    key_payload = json.dumps(
        {
            "model": model,
            "leader": leader,
            "definition": definition,
            "translations": translations,
            "known_contexts": known_contexts,
            "candidates": [
                {"id": item.id, "surface": item.surface, "sentence": item.sentence}
                for item in candidates
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
    path = cache_dir / f"context-approval-{user_id}.json"
    cache = _read_cache_file(path)
    cached = cache.get(key)
    if cached is not None:
        return set(cached)
    if not api_key:
        return None
    approved = approve_context_candidates(
        leader=leader,
        definition=definition,
        translations=translations,
        known_contexts=known_contexts,
        candidates=candidates,
        api_key=api_key,
        model=model,
    )
    cache = _read_cache_file(path)
    cache[key] = sorted(approved)
    _write_cache_file(path, cache)
    return approved


def decrypt_mirror_collection(
    database: sqlite3.Connection, user_id: int
) -> bytes | None:
    """Return the stored AnkiWeb collection mirror, or None when unavailable."""
    account = database.execute(
        "SELECT mirror_path, mirror_nonce, mirror_key_version FROM anki_accounts WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not account or not account["mirror_path"]:
        return None
    mirror = Path(account["mirror_path"])
    if not mirror.is_file():
        return None
    try:
        return decrypt_value(
            EncryptedValue(
                mirror.read_bytes(),
                account["mirror_nonce"],
                int(account["mirror_key_version"]),
            ),
            user_id=user_id,
            field="collection_mirror",
            keys=load_credential_keys(),
        )
    except Exception:
        LOGGER.warning("Could not decrypt collection mirror for user %s", user_id)
        return None


def refresh_article_contexts(
    database: sqlite3.Connection,
    user_id: int,
    *,
    card_ids: set[str] | None = None,
    data_dir: Path | None = None,
) -> set[str]:
    """Mine uploaded PDFs for extra meaning-only contexts.

    Exact occurrences plus imprecise word-formation matches (impair -> impaired)
    are collected, then the semantic model approves only the sentences that teach
    the card's exact sense. Without an API key a conservative offline fallback
    keeps the legacy lexical-overlap selection.
    """
    parameters: list[Any] = [user_id]
    card_filter = ""
    if card_ids is not None:
        if not card_ids:
            return set()
        placeholders = ",".join("?" for _ in card_ids)
        card_filter = f" AND id IN ({placeholders})"
        parameters.extend(sorted(card_ids))
    cards = database.execute(
        f"""SELECT * FROM cards
            WHERE user_id = ? AND semantic_version = 1{card_filter}
            ORDER BY created_at""",
        parameters,
    ).fetchall()
    documents = database.execute(
        """SELECT id, user_id, name, source_path, stored_path, text_path FROM documents
           WHERE user_id = ? AND kind = 'pdf' ORDER BY created_at""",
        (user_id,),
    ).fetchall()
    parsed_documents = []
    for document in documents:
        try:
            parsed = load_or_extract_document_text(database, document)
            parsed_documents.append((document, parsed))
        except (OSError, PdfReadError, RuntimeError, TypeError, ValueError) as exc:
            LOGGER.warning(
                "Could not load article text for %s: %s", document["name"], exc
            )
            continue

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("OPENROUTER_SEMANTIC_MODEL", DEFAULT_SEMANTIC_MODEL)
    word_indexes: dict[str, DocumentWordIndex] = {}
    changed: set[str] = set()
    for card in cards:
        contexts = [
            item
            for item in json.loads(card["contexts_json"] or "[]")
            if isinstance(item, dict)
        ]
        retained = [item for item in contexts if item.get("source") != "article_context"]
        targets = {
            " ".join(str(value).casefold().split())
            for value in (
                str(card["lemma"] or ""),
                str(card["family_key"] or ""),
                *(str(item.get("target") or "") for item in retained),
            )
            if value.strip()
        }
        known_sentences = {
            " ".join(str(item.get("sentence") or "").casefold().split())
            for item in retained
        }
        candidates: list[dict[str, Any]] = []
        for document, parsed in parsed_documents:
            index = word_indexes.get(document["id"])
            if index is None:
                index = document_word_index(parsed)
                word_indexes[document["id"]] = index
            collected: list[tuple[bool, TargetContext]] = [
                (True, occurrence)
                for occurrence in find_article_contexts(parsed, sorted(targets))
            ]
            collected.extend(
                (False, occurrence)
                for occurrence in find_variant_article_contexts(
                    parsed,
                    sorted(targets),
                    index=index,
                )
            )
            for exact, occurrence in collected[:CONTEXT_CANDIDATES_PER_DOCUMENT]:
                sentence_key = " ".join(occurrence.sentence.casefold().split())
                if sentence_key in known_sentences:
                    continue
                if not _article_context_is_readable(occurrence.sentence):
                    continue
                score = _article_context_score(
                    card,
                    retained,
                    occurrence.target,
                    occurrence.sentence,
                    targets,
                )
                candidates.append(
                    _article_context_candidate(
                        card,
                        document,
                        occurrence.target,
                        occurrence.sentence,
                        occurrence.source_page,
                        occurrence.id,
                        exact=exact,
                        score=score,
                    )
                )
                known_sentences.add(sentence_key)
        if not candidates:
            continue
        approved: set[str] | None = None
        if api_key:
            approval_candidates = candidates[:CONTEXT_APPROVAL_CANDIDATE_LIMIT]
            approval_request = {
                "leader": str(card["lemma"] or card["family_key"] or card["target"]),
                "definition": str(card["sense_definition_en"] or ""),
                "translations": json.loads(card["translations_json"]),
                "known_contexts": [
                    " ".join(str(item.get("sentence") or "").split())
                    for item in retained[:MAX_CONTEXT_APPROVAL_EXAMPLES]
                    if item.get("sentence")
                ],
                "candidates": [
                    ContextCandidate(
                        id=str(item["id"]),
                        surface=str(item["target"]),
                        sentence=str(item["sentence"]),
                    )
                    for item in approval_candidates
                ],
            }
            try:
                if data_dir is not None:
                    approved = context_approval_cached(
                        _llm_cache_dir(data_dir),
                        user_id,
                        api_key=api_key,
                        model=model,
                        **approval_request,
                    )
                else:
                    approved = approve_context_candidates(
                        api_key=api_key,
                        model=model,
                        **approval_request,
                    )
            except Exception:
                LOGGER.warning(
                    "Article-context approval failed for card %s; "
                    "falling back to lexical selection",
                    card["id"],
                )
                approved = None
        selected = _select_article_context_candidates(candidates, approved)
        updated = retained + selected
        if updated == contexts:
            continue
        database.execute(
            """UPDATE cards SET contexts_json = ?, csv_exported_at = NULL,
               apkg_exported_at = NULL, anki_synced_at = NULL
               WHERE id = ? AND user_id = ?""",
            (json.dumps(updated, ensure_ascii=False), card["id"], user_id),
        )
        changed.add(str(card["id"]))
    return changed


def _article_context_candidate(
    card: sqlite3.Row,
    document: sqlite3.Row,
    target: str,
    sentence: str,
    page: int,
    occurrence_id: str,
    *,
    exact: bool,
    score: int | None,
) -> dict[str, Any]:
    return {
        "id": f"article:{document['id']}:{occurrence_id}",
        "source": "article_context",
        "document_id": str(document["id"]),
        "document_name": str(document["name"]),
        "page": page,
        "directions": ["meaning"],
        "target": target,
        "sentence": sentence,
        "replacement": str(card["replacement"]),
        "lemma": str(card["lemma"]),
        "family_key": str(card["family_key"]),
        "part_of_speech": str(card["part_of_speech"]),
        "sense_definition_en": str(card["sense_definition_en"]),
        "score": score,
        "exact": exact,
    }


def _select_article_context_candidates(
    candidates: list[dict[str, Any]],
    approved: set[str] | None,
) -> list[dict[str, Any]]:
    def order_key(item: dict[str, Any]) -> tuple[Any, ...]:
        if approved is not None:
            return (
                item["id"] not in approved,
                not item["exact"],
                -(item["score"] or 0),
                item["document_id"],
                item["page"],
                item["id"],
            )
        score = item["score"]
        return (
            -score if score is not None else -1,
            item["document_id"],
            item["page"],
            item["id"],
        )

    if approved is None:
        candidates = [item for item in candidates if item["score"] is not None]
    selected = []
    per_document: dict[str, int] = {}
    for candidate in sorted(candidates, key=order_key):
        if per_document.get(candidate["document_id"], 0) >= 2:
            continue
        selected.append(candidate)
        per_document[candidate["document_id"]] = (
            per_document.get(candidate["document_id"], 0) + 1
        )
        if len(selected) == ARTICLE_CONTEXT_LIMIT:
            break
    return selected


def document_text_cache_path(document: sqlite3.Row) -> Path:
    return Path(document["stored_path"]).with_suffix(".text.json")


def write_document_text_cache(path: Path, document_text: DocumentText) -> None:
    payload = json.dumps(
        {
            "version": 1,
            "text": document_text.text,
            "page_ranges": document_text.page_ranges,
            "hard_page_starts": document_text.hard_page_starts,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_managed_file(path, payload)


def read_document_text_cache(path: Path) -> DocumentText:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Document-text cache must contain an object")
    if payload.get("version") != 1 or not isinstance(payload.get("text"), str):
        raise ValueError("Unsupported document-text cache")
    page_ranges = [tuple(item) for item in payload.get("page_ranges", [])]
    hard_page_starts = payload.get("hard_page_starts", [])
    if not all(
        len(item) == 2 and all(isinstance(value, int) for value in item)
        for item in page_ranges
    ) or not all(isinstance(value, bool) for value in hard_page_starts):
        raise ValueError("Invalid document-text cache")
    if len(page_ranges) != len(hard_page_starts):
        raise ValueError("Invalid document-text page metadata")
    return DocumentText(
        text=payload["text"],
        page_ranges=page_ranges,
        hard_page_starts=hard_page_starts,
    )


def load_or_extract_document_text(
    database: sqlite3.Connection,
    document: sqlite3.Row,
) -> DocumentText:
    cached_path = Path(document["text_path"]) if document["text_path"] else None
    if cached_path is not None and cached_path.is_file():
        try:
            return read_document_text_cache(cached_path)
        except (OSError, TypeError, ValueError):
            LOGGER.warning("Rebuilding invalid article-text cache %s", cached_path)

    pdf_path = Path(document["source_path"] or document["stored_path"])
    parsed = extract_document_text(pdf_path)
    cached_path = document_text_cache_path(document)
    write_document_text_cache(cached_path, parsed)
    database.execute(
        "UPDATE documents SET text_path = ? WHERE id = ? AND user_id = ?",
        (str(cached_path), document["id"], document["user_id"]),
    )
    return parsed


def remove_article_contexts_for_document(
    database: sqlite3.Connection,
    user_id: int,
    document_id: str,
) -> set[str]:
    changed: set[str] = set()
    cards = database.execute(
        """SELECT id, contexts_json FROM cards
           WHERE user_id = ? AND semantic_version = 1""",
        (user_id,),
    ).fetchall()
    for card in cards:
        contexts = json.loads(card["contexts_json"] or "[]")
        retained = [
            item
            for item in contexts
            if not (
                isinstance(item, dict)
                and item.get("source") == "article_context"
                and str(item.get("document_id")) == document_id
            )
        ]
        if retained == contexts:
            continue
        database.execute(
            """UPDATE cards SET contexts_json = ?, csv_exported_at = NULL,
               apkg_exported_at = NULL, anki_synced_at = NULL
               WHERE id = ? AND user_id = ?""",
            (json.dumps(retained, ensure_ascii=False), card["id"], user_id),
        )
        changed.add(str(card["id"]))
    return changed


def enrich_highlight_row(
    app: Flask,
    database: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    user_id: int,
    document_id: str,
    mine_article_contexts: bool = True,
) -> sqlite3.Row:
    timestamp = now()
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise MissingApiKeyError
    model = os.environ.get("OPENROUTER_SEMANTIC_MODEL", DEFAULT_SEMANTIC_MODEL)
    normalized_highlight = normalize_selected_text(str(row["target"]))
    candidates = find_cluster_candidates(database, user_id, normalized_highlight)
    analysis, _cached = cluster_analysis_cached(
        _llm_cache_dir(Path(app.config["DATA_DIR"])),
        user_id,
        model=model,
        target=str(row["target"]),
        normalized_target=normalized_highlight,
        sentence=str(row["sentence"]),
        candidates=candidates,
        api_key=api_key,
    )
    candidates_by_id = {item.cluster_id: item for item in candidates}
    selected_candidate = candidates_by_id.get(analysis.cluster_id)
    leader = (
        selected_candidate.leader
        if selected_candidate is not None
        else normalize_selected_text(analysis.leader) or normalized_highlight
    )
    translations = json.dumps(analysis.translations_ru, ensure_ascii=False)
    replacement = analysis.replacement_ru
    alternatives = "[]"
    source_context = {
        "id": str(row["id"]), "source": "user_pdf", "target": str(row["target"]),
        "sentence": str(row["sentence"]), "replacement": replacement,
        "translations": analysis.translations_ru, "lemma": leader,
        "family_key": leader, "part_of_speech": analysis.part_of_speech,
        "sense_definition_en": analysis.cluster_definition_en,
        "substitutes_en": analysis.source_distractors.substitutes_en,
        "related_en": analysis.source_distractors.related_en,
        "valid_substitutes_en": analysis.source_distractors.valid_substitutes_en,
        "valid_related_en": analysis.source_distractors.valid_related_en,
    }
    if selected_candidate is not None:
        existing = database.execute(
            "SELECT * FROM cards WHERE id = ? AND user_id = ?",
            (selected_candidate.cluster_id, user_id),
        ).fetchone()
        if existing is None:
            raise RuntimeError("Selected semantic card disappeared")
        contexts = json.loads(existing["contexts_json"] or "[]")
        known = {str(item.get("id")) for item in contexts if isinstance(item, dict)}
        known_sentences = {
            " ".join(str(item.get("sentence") or "").casefold().split())
            for item in contexts
            if isinstance(item, dict) and item.get("sentence")
        }
        source_sentence = " ".join(str(row["sentence"]).casefold().split())
        added_contexts = (
            []
            if source_sentence in known_sentences
            else [source_context]
        )
        contexts.extend(item for item in added_contexts if item["id"] not in known)
        card_translations = merge_semantic_translations(
            json.loads(existing["translations_json"]),
            analysis.translations_ru,
        )
        database.execute(
            """UPDATE cards SET contexts_json = ?, translations_json = ?,
               sense_definition_en = ?, csv_exported_at = NULL,
               apkg_exported_at = NULL, anki_synced_at = NULL WHERE id = ? AND user_id = ?""",
            (
                json.dumps(contexts, ensure_ascii=False),
                json.dumps(card_translations, ensure_ascii=False),
                analysis.cluster_definition_en,
                existing["id"],
                user_id,
            ),
        )
    else:
        card_id = str(uuid.uuid4())
        database.execute(
            """INSERT INTO cards
                (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, lemma, family_key,
                part_of_speech, sense_definition_en, contexts_json, semantic_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                card_id, user_id, document_id, leader,
                normalize_target(leader),
                row["sentence"], row["page"], translations, replacement, alternatives,
                leader, leader,
                analysis.part_of_speech.casefold(),
                analysis.cluster_definition_en, json.dumps([source_context], ensure_ascii=False), timestamp,
            ),
        )
        existing = database.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if existing is None:
        raise RuntimeError("Semantic card disappeared during enrichment")
    database.execute(
        """INSERT OR IGNORE INTO card_highlights (card_id, highlight_id)
           VALUES (?, ?)""",
        (existing["id"], row["id"]),
    )

    database.execute(
        """UPDATE highlights
           SET translations_json = ?, replacement = ?, alternatives_json = ?,
               status = 'ready', error = NULL, updated_at = ?
           WHERE id = ? AND user_id = ?""",
        (translations, replacement, alternatives, timestamp, row["id"], user_id),
    )
    account = database.execute(
        "SELECT state FROM anki_accounts WHERE user_id = ?", (user_id,)
    ).fetchone()
    if account is not None and account["state"] in {"connected", "syncing", "error"}:
        enqueue_sync_job(database, user_id, "card_saved", delay_seconds=30)
    database.commit()
    ready = database.execute(
        "SELECT * FROM highlights WHERE id = ? AND user_id = ?",
        (row["id"], user_id),
    ).fetchone()
    if ready is None:
        raise RuntimeError("Highlight disappeared during enrichment")
    if mine_article_contexts:
        try:
            changed = refresh_article_contexts(
                database,
                user_id,
                card_ids={str(existing["id"])},
                data_dir=Path(app.config["DATA_DIR"]),
            )
            if changed and account is not None and account["state"] in {
                "connected", "syncing", "error"
            }:
                enqueue_sync_job(
                    database, user_id, "article_contexts", delay_seconds=30
                )
            database.commit()
        except Exception:
            database.rollback()
            app.logger.exception(
                "Article context mining failed after highlight was saved"
            )
    return ready


def enqueue_reader_highlight_enrichment(
    app: Flask,
    *,
    highlight_id: str,
    user_id: int,
    document_id: str,
) -> None:
    jobs: set[str] = app.extensions["reader_highlight_jobs"]
    lock: threading.Lock = app.extensions["highlight_jobs_lock"]
    with lock:
        if highlight_id in jobs:
            return
        jobs.add(highlight_id)

    def run() -> None:
        database = sqlite3.connect(app.config["DATABASE"], timeout=30)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        try:
            row = database.execute(
                """SELECT * FROM highlights
                   WHERE id = ? AND user_id = ? AND document_id = ?""",
                (highlight_id, user_id, document_id),
            ).fetchone()
            if row is None or row["status"] == "ready":
                return
            enrich_highlight_row(
                app,
                database,
                row,
                user_id=user_id,
                document_id=document_id,
            )
        except MissingApiKeyError:
            app.logger.error("OPENROUTER_API_KEY is missing; discarding highlight")
            if "row" in locals() and row is not None:
                delete_highlight_rows(database, [row], user_id=user_id)
        except Exception:
            app.logger.exception("Automatic highlight enrichment failed")
            if "row" in locals() and row is not None:
                delete_highlight_rows(database, [row], user_id=user_id)
        finally:
            database.close()
            with lock:
                jobs.discard(highlight_id)

    if app.config["PROCESS_DOCUMENTS_INLINE"]:
        run()
    else:
        executor: ThreadPoolExecutor = app.extensions["highlight_executor"]
        executor.submit(run)


def write_pdf_without_native_highlights(source: Path, destination: Path) -> None:
    reader = PdfReader(source)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    for page in writer.pages:
        _remove_embedded_spen_highlights(page, writer)
        annotations = page.get("/Annots")
        if annotations is not None:
            kept = ArrayObject()
            for reference in annotations:
                annotation = reference.get_object()
                if annotation.get("/Subtype") != "/Highlight":
                    kept.append(reference)
            if kept:
                page[NameObject("/Annots")] = kept
            elif NameObject("/Annots") in page:
                del page[NameObject("/Annots")]

    writer.compress_identical_objects(
        remove_duplicates=False,
        remove_unreferenced=True,
    )

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


def _remove_embedded_spen_highlights(page: Any, writer: PdfWriter) -> None:
    contents = page.get_contents()
    if contents is None or b"/SPenSDK_PAGE_LIST" not in contents.get_data():
        return
    stream = ContentStream(contents, writer)
    kept_operations: list[tuple[list[Any], bytes]] = []
    marker_depth = 0
    removed = False
    for operands, operator in stream.operations:
        if marker_depth:
            if operator in {b"BMC", b"BDC"}:
                marker_depth += 1
            elif operator == b"EMC":
                marker_depth -= 1
            continue
        if (
            operator in {b"BMC", b"BDC"}
            and operands
            and str(operands[0]) == "/SPenSDK_PAGE_LIST"
        ):
            marker_depth = 1
            removed = True
            continue
        kept_operations.append((operands, operator))
    if not removed:
        return
    stream.operations = kept_operations
    page.replace_contents(stream)

    used_xobjects = {
        str(operands[0])
        for operands, operator in kept_operations
        if operator == b"Do" and operands
    }
    resources = page.get("/Resources")
    if resources is None:
        return
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return
    for name in list(xobjects.keys()):
        if str(name).startswith("/FXX") and str(name) not in used_xobjects:
            del xobjects[name]


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
                deleted_paths = app.extensions["deleted_document_paths"].pop(
                    document_id,
                    None,
                )
            if deleted_paths:
                remove_managed_files(Path(app.config["DATA_DIR"]), deleted_paths)

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

        document_text = load_or_extract_document_text(database, document)
        database.commit()
        extracted = extract_highlights(source_path, document_text=document_text)
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
            reader_candidates = database.execute(
                """SELECT target, rects_json FROM highlights
                   WHERE user_id = ? AND document_id = ? AND page = ?
                     AND source = 'reader'""",
                (user_id, document_id, item.context.source_page),
            ).fetchall()
            duplicate_reader_highlight = any(
                normalize_target(candidate["target"])
                == normalize_target(item.context.target)
                and highlight_rects_match(
                    rects,
                    clean_highlight_rects(json.loads(candidate["rects_json"])),
                )
                for candidate in reader_candidates
            )
            if duplicate_reader_highlight:
                continue
            identity = f"{item.context.source_page}:{rects_json}:{normalize_target(item.context.target)}"
            try:
                namespace = uuid.UUID(document_id)
            except ValueError:
                namespace = uuid.NAMESPACE_URL
                identity = f"{document_id}:{identity}"
            highlight_id = str(uuid.uuid5(namespace, identity))
            deleted = database.execute(
                """SELECT 1 FROM deleted_highlights
                   WHERE highlight_id = ? AND user_id = ? AND document_id = ?""",
                (highlight_id, user_id, document_id),
            ).fetchone()
            if deleted is not None:
                continue
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

        discarded = 0
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
                    mine_article_contexts=False,
                )
            except MissingApiKeyError:
                discarded += 1
                app.logger.error(
                    "OPENROUTER_API_KEY is missing; discarding imported highlight"
                )
                delete_highlight_rows(database, [row], user_id=user_id)
            except Exception:
                discarded += 1
                app.logger.exception("Imported highlight enrichment failed")
                delete_highlight_rows(database, [row], user_id=user_id)

        article_context_cards = refresh_article_contexts(
            database, user_id, data_dir=Path(app.config["DATA_DIR"])
        )
        if article_context_cards:
            account = database.execute(
                "SELECT state FROM anki_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()
            if account is not None and account["state"] in {"connected", "syncing", "error"}:
                enqueue_sync_job(database, user_id, "article_contexts", delay_seconds=30)

        imported_count = database.execute(
            """SELECT COUNT(*) FROM highlights
               WHERE document_id = ? AND user_id = ? AND source = 'pdf_import'""",
            (document_id, user_id),
        ).fetchone()[0]
        database.execute(
            """UPDATE documents
               SET highlight_status = 'ready', highlight_error = NULL,
                   highlight_processed_at = ?, imported_highlight_count = ?
               WHERE id = ? AND user_id = ?""",
            (now(), imported_count, document_id, user_id),
        )
        if discarded:
            app.logger.warning(
                "Discarded %s highlights after enrichment retries", discarded
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


def remove_managed_files(data_dir: Path, paths: list[str | None]) -> None:
    root = data_dir.resolve()
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path).resolve()
        if path == root or root not in path.parents:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        try:
            path.parent.rmdir()
        except OSError:
            pass


def replace_managed_file(destination: Path, content: bytes) -> None:
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.stem}-",
        suffix=destination.suffix,
        dir=destination.parent,
        delete=False,
    )
    temporary = Path(temporary_handle.name)
    try:
        with temporary_handle:
            temporary_handle.write(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


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
        if card["semantic_version"] == 1:
            for row in semantic_card_rows(card):
                writer.writerow(row)
            continue
        translations = json.loads(card["translations_json"])
        alternatives = json.loads(card["alternatives_json"])
        front = emphasize_target(card["sentence"], card["target"], html.escape(card["target"]))
        back = "<br>".join(f"• {html.escape(value)}" for value in translations)
        tag = re.sub(r"[^A-Za-z0-9_:-]+", "_", Path(card["document_name"]).stem).strip("_") or "article"
        common = f"article::{tag} page::{card['page']} anki_papers::{card['id']}"
        writer.writerow({"Front": front, "Back": back, "Tags": f"{common} card::meaning"})
        replacement = f"<b>{html.escape(card['replacement'])}</b>"
        recall = emphasize_target(card["sentence"], card["target"], replacement, replacement_is_html=True)
        if alternatives:
            recall += f"<br><small>Нельзя использовать: {', '.join(html.escape(value) for value in alternatives)}</small>"
        writer.writerow(
            {"Front": recall, "Back": f"<b>{html.escape(card['target'])}</b>", "Tags": f"{common} card::recall"}
        )
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def semantic_card_rows(card: sqlite3.Row) -> list[dict[str, str]]:
    """Two fields only: works in CSV and APKG Basic-like note types."""
    contexts = json.loads(card["contexts_json"] or "[]")
    if not contexts:
        raise ValueError("Semantic card has no contexts")
    translations_values = json.loads(card["translations_json"])
    fallback_replacement = str(translations_values[0])
    def render(selected: list[dict[str, Any]]) -> list[dict[str, str]]:
        rendered = []
        for item in selected:
            target = str(item["target"])
            sentence = str(item["sentence"])
            replacement = _compact_semantic_replacement(
                str(item["replacement"]),
                fallback=fallback_replacement,
                english_surface=target,
            )
            rendered.append({
                "front": emphasize_target(sentence, target, html.escape(target)),
                "recall": emphasize_target(
                    sentence,
                    target,
                    f"<b>{html.escape(replacement)}</b>",
                    replacement_is_html=True,
                ) + _semantic_recall_distractors_html(item),
                "answer": html.escape(target),
                "source": html.escape(str(item["source"])),
            })
        return rendered

    meaning_rendered = render(
        [item for item in contexts if _semantic_context_enabled(item, "meaning")]
    )
    recall_rendered = render(
        [item for item in contexts if _semantic_context_enabled(item, "recall")]
    )
    if not meaning_rendered or not recall_rendered:
        raise ValueError("Semantic card has no contexts for one direction")
    meaning_payload = html.escape(
        json.dumps(meaning_rendered, ensure_ascii=False), quote=True
    )
    recall_payload = html.escape(
        json.dumps(recall_rendered, ensure_ascii=False), quote=True
    )
    key = html.escape(str(card["id"]), quote=True)
    front = _semantic_front_html(meaning_payload, key, "front", meaning_rendered[0])
    recall = _semantic_front_html(recall_payload, key, "recall", recall_rendered[0])
    translations = "<br>".join(
        f"• {html.escape(value)}" for value in translations_values
    )
    sense = html.escape(str(card["sense_definition_en"]))
    family_tag = re.sub(
        r"[^A-Za-z0-9_:-]+",
        "_",
        str(card["family_key"] or card["lemma"]),
    ).strip("_") or "word"
    common = f"anki_papers::{card['id']} semantic::v1 family::{family_tag}"
    meaning_back = _semantic_back_html(
        meaning_payload,
        key,
        "front",
        meaning_rendered[0],
        f"<br>{translations}<br><small>{sense}</small>",
    )
    recall_back = _semantic_back_html(
        recall_payload,
        key,
        "recall",
        recall_rendered[0],
        "",
    )
    return [
        {"Front": front, "Back": meaning_back, "Tags": f"{common} card::meaning"},
        {"Front": recall, "Back": recall_back, "Tags": f"{common} card::recall"},
    ]


def _semantic_context_enabled(context: dict[str, Any], direction: str) -> bool:
    directions = context.get("directions")
    return not directions or direction in directions


def _semantic_recall_distractors_html(context: dict[str, Any]) -> str:
    valid = context.get("valid_substitutes_en") or []
    related = (
        context.get("valid_related_en")
        or context.get("related_but_uninsertable_en")
        or context.get("meaning_related_non_substitutes_en")
        or []
    )
    valid_normalized = {str(value).casefold() for value in valid}
    related = [
        value for value in related if str(value).casefold() not in valid_normalized
    ]
    hints = []
    if valid:
        values = ", ".join(html.escape(str(value)) for value in valid)
        hints.append(f"Подходит, но не целевой ответ: {values}")
    if related:
        values = ", ".join(html.escape(str(value)) for value in related)
        hints.append(f"Близко по смыслу: {values}")
    if not hints:
        return ""
    return "<br><small>" + "<br>".join(hints) + "</small>"


def _semantic_front_html(
    payload: str,
    key: str,
    side: str,
    fallback: dict[str, str],
) -> str:
    # sessionStorage keeps question and answer on same context in clients that reload the page.
    fallback_html = fallback[side]
    return (
        f'<div class="anki-papers-semantic" data-key="{key}" data-side="{side}" '
        f'data-contexts="{payload}">{fallback_html}</div><script>(function(){{'
        'var root=document.currentScript.previousElementSibling,items=JSON.parse(root.dataset.contexts),'
        'key="anki-papers-context:"+root.dataset.key+":"+root.dataset.side,stored=null;'
        'try{stored=JSON.parse(sessionStorage.getItem(key)||"null")}catch(e){}'
        'var now=Date.now(),valid=stored&&stored.until>now&&Number.isInteger(stored.index)&&'
        'stored.index>=0&&stored.index<items.length,index=valid?stored.index:Math.floor(Math.random()*items.length);'
        'try{sessionStorage.setItem(key,JSON.stringify({index:index,until:now+120000}))}catch(e){}'
        'var item=items[index];root.innerHTML=item[root.dataset.side];'
        '})();</script>'
    )


def _semantic_back_html(
    payload: str,
    key: str,
    side: str,
    fallback: dict[str, str],
    details: str,
) -> str:
    return (
        f'<div class="anki-papers-semantic-answer" data-key="{key}" data-side="{side}" '
        f'data-contexts="{payload}"><b>{fallback["answer"]}</b></div>'
        '<script>(function(){var root=document.currentScript.previousElementSibling,'
        'items=JSON.parse(root.dataset.contexts),key="anki-papers-context:"+root.dataset.key+'
        '":"+root.dataset.side,stored=null;try{stored=JSON.parse(sessionStorage.getItem(key)||"null")}'
        'catch(e){}var valid=stored&&Number.isInteger(stored.index)&&stored.index>=0&&'
        'stored.index<items.length,index=valid?stored.index:0;root.innerHTML="<b>"+'
        'items[index].answer+"</b>";})();</script>'
        f"{details}"
    )


def emphasize_target(sentence: str, target: str, replacement: str, *, replacement_is_html: bool = False) -> str:
    match = re.search(re.escape(target), sentence, flags=re.IGNORECASE)
    if not match:
        return html.escape(sentence)
    selected = replacement if replacement_is_html else f"<b>{replacement}</b>"
    return f"{html.escape(sentence[:match.start()])}{selected}{html.escape(sentence[match.end():])}"


def _compact_semantic_replacement(
    value: str,
    *,
    fallback: str,
    english_surface: str,
) -> str:
    cleaned = " ".join(value.split())
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9-]+", cleaned)
    surface_words = re.findall(r"[A-Za-z0-9-]+", english_surface)
    maximum_words = min(8, max(5, len(surface_words) * 2 + 1))
    if len(words) > maximum_words or any(
        mark in cleaned for mark in ("\n", ".", ";", "!", "?")
    ):
        return fallback
    return cleaned or fallback


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
