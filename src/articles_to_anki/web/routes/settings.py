"""web / routes / settings."""
from __future__ import annotations

import json
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from articles_to_anki.cards.text import now
from articles_to_anki.jobs.rebuild_status import _rebuild_deck_name_hint
from articles_to_anki.security import encrypt_value, verify_password
from articles_to_anki.storage.database import get_database
from articles_to_anki.storage.files import remove_managed_files
from articles_to_anki.sync_queue import enqueue_sync_job
from articles_to_anki.sync_ui import build_sync_status
from articles_to_anki.web.auth import (
    ankiweb_enabled_for_user,
    login_required,
    require_csrf,
)


def register(app: Flask) -> None:
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
        rebuild_job = database.execute(
            """SELECT * FROM rebuild_jobs
               WHERE user_id = ? ORDER BY created_at DESC LIMIT 1""",
            (session["user_id"],),
        ).fetchone()
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
            rebuild_job=rebuild_job,
            rebuild_deck_name=_rebuild_deck_name_hint(
                rebuild_job["created_at"] if rebuild_job else None
            ),
            ankiweb_connected=bool(account and account["mirror_path"]),
        )

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
