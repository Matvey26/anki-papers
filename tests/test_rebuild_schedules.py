from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

from support_rebuild import (
    _KEY,
    _REBUILD_DECK_NAME_RE,
    _add_highlight,
    _install_ankiweb_mirror,
    _read_rebuilt,
    _site_card_id,
    _start_rebuild,
)
from support_rebuild_fixtures import _rebuild_download
from support_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.rebuild as ref_rebuild


def test_rebuild_carries_schedule_from_selected_ankiweb_deck(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    monkeypatch.setenv(
        "ANKI_CREDENTIAL_KEY", base64.urlsafe_b64encode(_KEY).decode()
    )
    app = make_app(tmp_path, REBUILD_INLINE=True)
    client = app.test_client()
    response = identify(client)

    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "Статья загружена" in response.text
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents WHERE kind = 'pdf'").fetchone()[0]
    client.post(f"/article/{document_id}/read", data={"csrf_token": csrf(response), "read": "1"})
    _add_highlight(client, document_id)
    site_id = _site_card_id(tmp_path)
    _install_ankiweb_mirror(tmp_path, site_id)

    _start_rebuild(client, tmp_path, deck_id="2")
    payload, download = _rebuild_download(client)
    assert payload["state"] == "succeeded"

    database = _read_rebuilt(tmp_path, download)
    try:
        assert database.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 2
        rows = database.execute(
            "SELECT n.tags, c.type, c.queue, c.due, c.ivl, c.factor, c.reps, c.lapses "
            "FROM notes n JOIN cards c ON c.nid = n.id"
        ).fetchall()
        by_direction = {
            "meaning" if "card::meaning" in row[0] else "recall": row
            for row in rows
        }
        meaning = by_direction["meaning"]
        assert meaning[1:] == (2, 2, 3333, 25, 2500, 6, 1)
        recall = by_direction["recall"]
        assert recall[1:] == (3, 1, 4444, 7, 2200, 3, 0)
        for row in rows:
            assert f"anki_papers::{site_id}" in row[0]
        deck = json.loads(database.execute("SELECT decks FROM col").fetchone()[0])
        assert any(
            _REBUILD_DECK_NAME_RE.match(saved["name"]) is not None
            and int(key) != 1
            for key, saved in deck.items()
        )
        notes = database.execute("SELECT mid, flds FROM notes").fetchall()
        assert len({mid for mid, _ in notes}) == 1
        assert all("\x1f" in fields and fields.strip() for _, fields in notes)
    finally:
        database.close()


def test_rebuild_rejects_deck_outside_account(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    monkeypatch.setenv(
        "ANKI_CREDENTIAL_KEY", base64.urlsafe_b64encode(_KEY).decode()
    )
    app = make_app(tmp_path)
    client = app.test_client()
    response = identify(client)

    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents WHERE kind = 'pdf'").fetchone()[0]
    client.post(f"/article/{document_id}/read", data={"csrf_token": csrf(response), "read": "1"})
    _add_highlight(client, document_id)
    site_id = _site_card_id(tmp_path)
    _install_ankiweb_mirror(tmp_path, site_id)

    settings_page = client.get("/settings")
    rejected = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(settings_page), "deck_id": "999"},
        follow_redirects=True,
    )
    assert rejected.status_code == 200
    assert "Выбранной колоды нет в аккаунте AnkiWeb" in rejected.text
    assert "Колода AnkiWeb для переноса прогресса" in settings_page.text
    assert "old_deck" not in settings_page.text


def test_rebuild_fresh_without_old_deck_is_deterministic(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    monkeypatch.setattr(
        ref_rebuild, "_deck_name",
        lambda value: "Anki Papers (пересборка) 2026-01-01 00:00",
    )
    app = make_app(tmp_path, REBUILD_INLINE=True)
    client = app.test_client()
    response = identify(client)

    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents WHERE kind = 'pdf'").fetchone()[0]
    client.post(f"/article/{document_id}/read", data={"csrf_token": csrf(response), "read": "1"})
    _add_highlight(client, document_id)

    _start_rebuild(client, tmp_path)
    _payload, first = _rebuild_download(client)
    _start_rebuild(client, tmp_path)
    _payload, second = _rebuild_download(client)
    assert first.data == second.data

    database = _read_rebuilt(tmp_path, first)
    try:
        rows = database.execute(
            "SELECT n.tags, c.type, c.queue FROM notes n JOIN cards c ON c.nid = n.id"
        ).fetchall()
        assert len(rows) == 2
        for tags, kind, queue in rows:
            assert "semantic::v1" in tags
            assert "anki_papers::" in tags
            assert (kind, queue) == (0, 0)
    finally:
        database.close()
