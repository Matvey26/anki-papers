from __future__ import annotations

import base64
import json
import re
import sqlite3
from pathlib import Path

from anki_papers_sync_worker.official import (
    AdapterResult,
    AuthenticationError,
    PermanentSyncError,
)
from anki_papers_sync_worker.worker import SyncWorker

from articles_to_anki.webapp import create_app

KEY = bytes(range(32))
PASSWORD = "correct horse battery staple"


def csrf(response) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match
    return match.group(1).decode()


class FakeAdapter:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.sync_calls = 0

    def connect(self, path, username, password, cards, known_links=None):
        self.connect_calls += 1
        assert username == "anki@example.com"
        assert password == "anki-password"
        path.write_bytes(b"downloaded remote collection")
        return AdapterResult(
            hkey="fresh-hkey",
            decks=[{"id": 1, "name": "Default"}, {"id": 42, "name": "Papers"}],
            links=[],
            existing=0,
            missing=len(cards) * 2,
        )

    def sync(self, path, hkey, deck_id, cards, known_links=None):
        self.sync_calls += 1
        assert path.read_bytes() == b"downloaded remote collection"
        assert hkey == "fresh-hkey"
        assert deck_id == 42
        path.write_bytes(b"incrementally synced collection")
        links = [
            {
                "site_card_id": card["id"],
                "direction": direction,
                "note_id": 100 + index * 2 + (direction == "recall"),
                "note_guid": f"guid-{index}-{direction}",
            }
            for index, card in enumerate(cards)
            for direction in ("meaning", "recall")
        ]
        return AdapterResult(
            hkey=hkey,
            decks=[{"id": 42, "name": "Papers"}],
            links=links,
            added=len(links),
        )

    def login(self, path, username, password):
        return "fresh-hkey"


class AuthFailAdapter(FakeAdapter):
    def connect(self, path, username, password, cards, known_links=None):
        raise AuthenticationError("hidden details")


class ConfigurationFailAdapter(FakeAdapter):
    def connect(self, path, username, password, cards, known_links=None):
        raise PermanentSyncError("managed_notetype_invalid")


def make_connected_web_state(tmp_path: Path):
    key_text = base64.urlsafe_b64encode(KEY).decode()
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SESSION_COOKIE_SECURE": False,
            "DATA_DIR": tmp_path,
            "DATABASE": tmp_path / "app.sqlite3",
            "AUTO_PROCESS_UPLOADS": False,
            "ANKIWEB_ALLOWED_USERS": "*",
            "ANKI_CREDENTIAL_KEYS": {1: KEY},
        }
    )
    client = app.test_client()
    register = client.get("/register")
    dashboard = client.post(
        "/register",
        data={
            "csrf_token": csrf(register),
            "username": "reader",
            "password": PASSWORD,
            "password_confirmation": PASSWORD,
        },
        follow_redirects=True,
    )
    settings = client.get("/settings")
    response = client.post(
        "/settings/anki/connect",
        data={
            "csrf_token": csrf(settings),
            "site_password": PASSWORD,
            "ankiweb_id": "anki@example.com",
            "ankiweb_password": "anki-password",
        },
        follow_redirects=True,
    )
    assert "поставлены в очередь" in response.text
    return app, client, key_text, dashboard


def insert_card(database_path: Path) -> str:
    with sqlite3.connect(database_path) as database:
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
        database.execute(
            """INSERT INTO documents
               (id, user_id, kind, name, stored_path, page_count, size, created_at)
               VALUES ('doc-1', ?, 'pdf', 'paper.pdf', '/tmp/paper.pdf', 1, 1, '2026-01-01')""",
            (user_id,),
        )
        database.execute(
            """INSERT INTO cards
               (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, created_at)
               VALUES ('site-card-1', ?, 'doc-1', 'robust', 'robust',
                       'A robust result.', 1, '["надёжный", "стойкий"]',
                       'надёжный', '["strong"]', '2026-01-01')""",
            (user_id,),
        )
    return "site-card-1"


def test_connect_preview_then_incremental_sync(tmp_path: Path) -> None:
    _app, client, _key_text, _dashboard = make_connected_web_state(tmp_path)
    insert_card(tmp_path / "app.sqlite3")
    adapter = FakeAdapter()
    worker = SyncWorker(
        tmp_path / "app.sqlite3", tmp_path, keys={1: KEY}, adapter=adapter
    )
    worker.startup_cleanup()
    assert worker.run_once()
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        account = database.execute(
            """SELECT state, mirror_path, preview_missing, available_decks_json
               FROM anki_accounts"""
        ).fetchone()
        assert account[0] == "awaiting_deck"
        assert account[2] == 2
        assert json.loads(account[3])[1]["name"] == "Papers"
        encrypted_mirror = Path(account[1]).read_bytes()
        assert b"downloaded remote collection" not in encrypted_mirror
        credentials_blob = b"".join(
            value for value in database.execute(
                """SELECT ankiweb_id_ciphertext, password_ciphertext, hkey_ciphertext
                   FROM user_credentials"""
            ).fetchone()
        )
        assert b"anki@example.com" not in credentials_blob
        assert b"anki-password" not in credentials_blob
        assert b"fresh-hkey" not in credentials_blob

    settings = client.get("/settings")
    assert "Выберите целевую колоду" in settings.text
    assert "Коллекция загружена" in settings.text
    assert "Подключение AnkiWeb" in settings.text
    assert "awaiting_deck" not in settings.text
    assert ">succeeded<" not in settings.text
    selected = client.post(
        "/settings/anki/deck",
        data={"csrf_token": csrf(settings), "deck_id": "42"},
        follow_redirects=True,
    )
    assert "Карточки синхронизируются" in selected.text
    assert worker.run_once()
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT state FROM anki_accounts").fetchone()[0] == "connected"
        assert database.execute("SELECT COUNT(*) FROM anki_note_links").fetchone()[0] == 2
        assert database.execute("SELECT anki_synced_at FROM cards").fetchone()[0]
        mirror_path = Path(database.execute("SELECT mirror_path FROM anki_accounts").fetchone()[0])
        assert b"incrementally synced collection" not in mirror_path.read_bytes()
    assert adapter.connect_calls == 1
    assert adapter.sync_calls == 1

    settings = client.get("/settings")
    disconnected = client.post(
        "/settings/anki/disconnect",
        data={"csrf_token": csrf(settings)},
        follow_redirects=True,
    )
    assert "секреты, зеркало" in disconnected.text
    assert not mirror_path.exists()
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM user_credentials").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM anki_accounts").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM anki_note_links").fetchone()[0] == 0
        assert database.execute(
            "SELECT COUNT(*) FROM sync_jobs WHERE state IN ('queued', 'running')"
        ).fetchone()[0] == 0


def test_two_auth_failures_require_reconnect_without_leaking_error(tmp_path: Path) -> None:
    make_connected_web_state(tmp_path)
    worker = SyncWorker(
        tmp_path / "app.sqlite3", tmp_path, keys={1: KEY}, adapter=AuthFailAdapter()
    )
    assert worker.run_once()
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute(
            "UPDATE sync_jobs SET run_after = '2000-01-01' WHERE state = 'queued'"
        )
    assert worker.run_once()
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        credentials = database.execute(
            "SELECT state, auth_failures FROM user_credentials"
        ).fetchone()
        account = database.execute("SELECT state, last_error FROM anki_accounts").fetchone()
        job = database.execute("SELECT state, error_code FROM sync_jobs").fetchone()
    assert credentials == ("needs_reconnect", 2)
    assert account == ("needs_reconnect", "Нужно переподключить AnkiWeb.")
    assert job == ("failed", "auth")
    assert "hidden details" not in account[1]


def test_configuration_failure_records_safe_actionable_reason(tmp_path: Path) -> None:
    make_connected_web_state(tmp_path)
    worker = SyncWorker(
        tmp_path / "app.sqlite3",
        tmp_path,
        keys={1: KEY},
        adapter=ConfigurationFailAdapter(),
    )

    assert worker.run_once()

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        account = database.execute(
            "SELECT state, last_error FROM anki_accounts"
        ).fetchone()
        job = database.execute("SELECT state, error_code FROM sync_jobs").fetchone()
    assert account == (
        "error",
        "Тип карточек «Anki Papers» изменён вручную и несовместим.",
    )
    assert job == ("failed", "configuration:managed_notetype_invalid")


def test_official_adapter_has_no_full_upload_path() -> None:
    source = (Path(__file__).parents[1] / "sync-worker/src/anki_papers_sync_worker/official.py").read_text()
    assert "upload=True" not in source
    assert "upload=False" in source


def test_mit_web_package_does_not_link_to_agpl_anki_package() -> None:
    web_source = Path(__file__).parents[1] / "src/articles_to_anki"
    for path in web_source.rglob("*.py"):
        source = path.read_text()
        assert "from anki " not in source
        assert "from anki." not in source
        assert "import anki" not in source
