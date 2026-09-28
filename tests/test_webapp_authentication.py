from __future__ import annotations

import sqlite3
from pathlib import Path

from support_webapp import csrf, identify, make_app

import articles_to_anki.web.routes.auth as ref_auth
from articles_to_anki.security import claim_token_digest


def test_existing_passwordless_profile_requires_one_time_claim(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute(
            "INSERT INTO users(username, created_at) VALUES ('same-user', '2026-01-01')"
        )
        user_id = database.execute(
            "SELECT id FROM users WHERE username = 'same-user'"
        ).fetchone()[0]
        database.execute(
            """INSERT INTO account_claim_tokens
               (id, user_id, token_hash, expires_at, created_at)
               VALUES ('claim-1', ?, ?, '2099-01-01', '2026-01-01')""",
            (user_id, claim_token_digest("one-time-secret")),
        )

    client = app.test_client()
    login = client.get("/login")
    rejected = client.post(
        "/login",
        data={"csrf_token": csrf(login), "username": "same-user", "password": "anything-long"},
        follow_redirects=True,
    )
    assert "claim-код" in rejected.text
    claim = client.get("/claim")
    claimed = client.post(
        "/claim",
        data={
            "csrf_token": csrf(claim),
            "username": "SAME-user",
            "claim_code": "one-time-secret",
            "password": "new secure password",
            "password_confirmation": "new secure password",
        },
        follow_redirects=True,
    )
    assert "Библиотека" in claimed.text
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute(
            "SELECT used_at IS NOT NULL FROM account_claim_tokens"
        ).fetchone()[0] == 1


def test_existing_database_adds_nullable_password_hash(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute(
            """CREATE TABLE users (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               username TEXT NOT NULL COLLATE NOCASE UNIQUE,
               created_at TEXT NOT NULL)"""
        )
        database.execute(
            "INSERT INTO users(username, created_at) VALUES ('old-user', '2026-01-01')"
        )

    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(users)")}
        username = database.execute("SELECT username FROM users").fetchone()[0]
    assert "password_hash" in columns
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        password_hash = database.execute("SELECT password_hash FROM users").fetchone()[0]
    assert username == "old-user"
    assert password_hash is None


def test_legacy_passwordless_session_is_forced_to_claim(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute(
            "INSERT INTO users(username, created_at) VALUES ('legacy', '2026-01-01')"
        )
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["user_id"] = user_id
        browser_session["username"] = "legacy"
    response = client.get("/dashboard", follow_redirects=True)
    assert "Claim-код" in response.text
    with client.session_transaction() as browser_session:
        assert "user_id" not in browser_session


def test_registration_requires_long_password_and_secure_cookie(tmp_path: Path) -> None:
    app = make_app(tmp_path, SESSION_COOKIE_SECURE=True)
    client = app.test_client()
    page = client.get("/register")
    rejected = client.post(
        "/register",
        data={
            "csrf_token": csrf(page),
            "username": "secure-user",
            "password": "too-short",
            "password_confirmation": "too-short",
        },
        follow_redirects=True,
    )
    assert "не менее 12" in rejected.text
    page = client.get("/register")
    accepted = client.post(
        "/register",
        data={
            "csrf_token": csrf(page),
            "username": "secure-user",
            "password": "long secure password",
            "password_confirmation": "long secure password",
        },
    )
    cookie = accepted.headers["Set-Cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_login_rate_limit_uses_username_or_ip(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ref_auth.time, "sleep", lambda _seconds: None)
    app = make_app(tmp_path)
    identify(app.test_client(), "rate-user")
    attacker = app.test_client()
    for _ in range(8):
        page = attacker.get("/login")
        attacker.post(
            "/login",
            data={
                "csrf_token": csrf(page),
                "username": "rate-user",
                "password": "incorrect password",
            },
        )
    page = attacker.get("/login")
    limited = attacker.post(
        "/login",
        data={
            "csrf_token": csrf(page),
            "username": "different-user",
            "password": "incorrect password",
        },
    )
    assert limited.status_code == 429
