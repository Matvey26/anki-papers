"""web / auth."""
from __future__ import annotations

import functools
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from flask import Flask, Response, abort, flash, redirect, request, session, url_for

from articles_to_anki.cards.text import now
from articles_to_anki.storage.database import get_database


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
