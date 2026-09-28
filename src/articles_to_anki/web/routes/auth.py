"""web / routes / auth."""
from __future__ import annotations

import sqlite3
import time

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

from articles_to_anki.cards.text import now
from articles_to_anki.constants import USERNAME_RE
from articles_to_anki.security import (
    claim_token_digest,
    hash_password,
    password_needs_rehash,
    validate_password,
    verify_password,
)
from articles_to_anki.storage.database import get_database
from articles_to_anki.web.auth import (
    failed_login_count,
    login_is_rate_limited,
    login_required,
    record_login_attempt,
    require_csrf,
    start_user_session,
)


def register(app: Flask) -> None:
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
