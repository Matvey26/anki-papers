from __future__ import annotations

import base64
import io
import json
import os
import re
import sqlite3
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_apkg import write_apkg
from test_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.rebuild as rebuild_module
import articles_to_anki.webapp as webapp_module
from articles_to_anki.models import ClusterAnalysis, ClusterCandidate, RecallDistractors
from articles_to_anki.security import encrypt_value, load_credential_keys

_REBUILD_DECK_NAME_RE = re.compile(
    r"^Anki Papers \(пересборка\) \d{4}-\d{2}-\d{2} \d{2}:\d{2}$"
)

_KEY = bytes(range(32))


def _full_legacy_collection_bytes(tmp_path: Path, site_id: str) -> bytes:
    database = sqlite3.connect(tmp_path / "full-old.sqlite")
    database.executescript(
        """
        CREATE TABLE col (
            id INTEGER PRIMARY KEY, crt INTEGER NOT NULL, mod INTEGER NOT NULL,
            scm INTEGER NOT NULL, ver INTEGER NOT NULL, dty INTEGER NOT NULL,
            usn INTEGER NOT NULL, ls INTEGER NOT NULL, conf TEXT NOT NULL,
            models TEXT NOT NULL, decks TEXT NOT NULL, dconf TEXT NOT NULL,
            tags TEXT NOT NULL
        );
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY, guid TEXT NOT NULL, mid INTEGER NOT NULL,
            mod INTEGER NOT NULL, usn INTEGER NOT NULL, tags TEXT NOT NULL,
            flds TEXT NOT NULL, sfld TEXT NOT NULL, csum INTEGER NOT NULL,
            flags INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY, nid INTEGER NOT NULL, did INTEGER NOT NULL,
            ord INTEGER NOT NULL, mod INTEGER NOT NULL, usn INTEGER NOT NULL,
            type INTEGER NOT NULL, queue INTEGER NOT NULL, due INTEGER NOT NULL,
            ivl INTEGER NOT NULL, factor INTEGER NOT NULL, reps INTEGER NOT NULL,
            lapses INTEGER NOT NULL, left INTEGER NOT NULL, odue INTEGER NOT NULL,
            odid INTEGER NOT NULL, flags INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE revlog (
            id INTEGER PRIMARY KEY, cid INTEGER NOT NULL, usn INTEGER NOT NULL,
            ease INTEGER NOT NULL, ivl INTEGER NOT NULL, lastIvl INTEGER NOT NULL,
            factor INTEGER NOT NULL, time INTEGER NOT NULL, type INTEGER NOT NULL
        );
        CREATE TABLE graves (
            id INTEGER PRIMARY KEY, oid INTEGER NOT NULL, type INTEGER NOT NULL,
            usn INTEGER NOT NULL
        );
        """
    )
    template = json.loads(
        (Path(__file__).parent.parent / "src/articles_to_anki/rebuild_template.json").read_text(
            encoding="utf-8"
        )
    )
    basic = next(mid for mid, model in template["models"].items() if len(model["flds"]) == 2)
    mod = int(time.time())
    database.execute(
        "INSERT INTO col VALUES (1, ?, ?, ?, ?, 0, -1, 0, ?, ?, ?, ?, ?)",
        (
            template["crt"],
            mod,
            mod * 1000,
            template["ver"],
            json.dumps(template["conf"]),
            json.dumps(template["models"]),
            json.dumps(template["decks"]),
            json.dumps(template["dconf"]),
            json.dumps(template["tags"]),
        ),
    )
    for index, (tags, kind, queue, due, ivl, factor, reps, lapses) in enumerate(
        [
            (f" anki_papers::{site_id} semantic::v1 card::meaning ", 2, 2, 3333, 25, 2500, 6, 1),
            (f" anki_papers::{site_id} semantic::v1 card::recall ", 3, 1, 4444, 7, 2200, 3, 0),
        ],
        start=1,
    ):
        database.execute(
            "INSERT INTO notes VALUES (?, 'g', ?, 1, 0, ?, 'front\\x1fback', 'front', 1, 0, '')",
            (index, basic, tags),
        )
        database.execute(
            "INSERT INTO cards VALUES (?, ?, 1, 0, 1, 0, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, '{}')",
            (index, index, kind, queue, due, ivl, factor, reps, lapses),
        )
    database.commit()
    database.close()
    content = (tmp_path / "full-old.sqlite").read_bytes()
    (tmp_path / "full-old.sqlite").unlink()
    return content


def _mirror_collection_bytes(tmp_path: Path, site_id: str) -> bytes:
    """Full modern collection with three decks and conflicting schedules.

    Deck "Default" carries the site's tags with wrong scheduling, deck
    "Papers" carries the correct meaning state and its subdeck "Papers::Advanced"
    the correct recall state. Choosing "Papers" must scope the transfer to the
    deck subtree and ignore "Default".
    """
    database = sqlite3.connect(tmp_path / "mirror-old.sqlite")
    database.executescript(
        """
        CREATE TABLE col (
            id INTEGER PRIMARY KEY, crt INTEGER NOT NULL, mod INTEGER NOT NULL,
            scm INTEGER NOT NULL, ver INTEGER NOT NULL, dty INTEGER NOT NULL,
            usn INTEGER NOT NULL, ls INTEGER NOT NULL, conf TEXT NOT NULL,
            models TEXT NOT NULL, decks TEXT NOT NULL, dconf TEXT NOT NULL,
            tags TEXT NOT NULL
        );
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY, guid TEXT NOT NULL, mid INTEGER NOT NULL,
            mod INTEGER NOT NULL, usn INTEGER NOT NULL, tags TEXT NOT NULL,
            flds TEXT NOT NULL, sfld TEXT NOT NULL, csum INTEGER NOT NULL,
            flags INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY, nid INTEGER NOT NULL, did INTEGER NOT NULL,
            ord INTEGER NOT NULL, mod INTEGER NOT NULL, usn INTEGER NOT NULL,
            type INTEGER NOT NULL, queue INTEGER NOT NULL, due INTEGER NOT NULL,
            ivl INTEGER NOT NULL, factor INTEGER NOT NULL, reps INTEGER NOT NULL,
            lapses INTEGER NOT NULL, left INTEGER NOT NULL, odue INTEGER NOT NULL,
            odid INTEGER NOT NULL, flags INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE revlog (
            id INTEGER PRIMARY KEY, cid INTEGER NOT NULL, usn INTEGER NOT NULL,
            ease INTEGER NOT NULL, ivl INTEGER NOT NULL, lastIvl INTEGER NOT NULL,
            factor INTEGER NOT NULL, time INTEGER NOT NULL, type INTEGER NOT NULL
        );
        CREATE TABLE graves (
            id INTEGER PRIMARY KEY, oid INTEGER NOT NULL, type INTEGER NOT NULL,
            usn INTEGER NOT NULL
        );
        """
    )
    template = json.loads(
        (Path(__file__).parent.parent / "src/articles_to_anki/rebuild_template.json").read_text(
            encoding="utf-8"
        )
    )
    basic = next(mid for mid, model in template["models"].items() if len(model["flds"]) == 2)
    base_deck = template["decks"]["1"]
    decks = {
        "1": {**base_deck, "name": "Default"},
        "2": {**base_deck, "name": "Papers"},
        "3": {**base_deck, "name": "Papers::Advanced"},
    }
    mod = int(time.time())
    database.execute(
        "INSERT INTO col VALUES (1, ?, ?, ?, ?, 0, -1, 0, ?, ?, ?, ?, ?)",
        (
            template["crt"],
            mod,
            mod * 1000,
            template["ver"],
            json.dumps(template["conf"]),
            json.dumps(template["models"]),
            json.dumps(decks),
            json.dumps(template["dconf"]),
            json.dumps(template["tags"]),
        ),
    )
    for index, (tags, kind, queue, due, ivl, factor, reps, lapses, deck) in enumerate(
        [
            (f" anki_papers::{site_id} semantic::v1 card::meaning ", 2, 2, 1111, 90, 2500, 20, 5, 1),
            (f" anki_papers::{site_id} semantic::v1 card::recall ", 3, 1, 2222, 3, 2200, 1, 0, 1),
            (f" anki_papers::{site_id} semantic::v1 card::meaning ", 2, 2, 3333, 25, 2500, 6, 1, 2),
            (f" anki_papers::{site_id} semantic::v1 card::recall ", 3, 1, 4444, 7, 2200, 3, 0, 3),
        ],
        start=1,
    ):
        database.execute(
            "INSERT INTO notes VALUES (?, 'g', ?, 1, 0, ?, 'front\\x1fback', 'front', 1, 0, '')",
            (index, basic, tags),
        )
        database.execute(
            "INSERT INTO cards VALUES (?, ?, ?, 0, 1, 0, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, '{}')",
            (index, index, deck, kind, queue, due, ivl, factor, reps, lapses),
        )
    database.commit()
    database.close()
    content = (tmp_path / "mirror-old.sqlite").read_bytes()
    (tmp_path / "mirror-old.sqlite").unlink()
    return content


def _install_ankiweb_mirror(tmp_path: Path, site_id: str) -> None:
    """Persist an encrypted mirror and its deck list for the test user (id 1)."""
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
        keys = load_credential_keys()
        encrypted = encrypt_value(
            _mirror_collection_bytes(tmp_path, site_id),
            user_id=user_id,
            field="collection_mirror",
            keys=keys,
        )
        mirror_path = tmp_path / "mirror.enc"
        mirror_path.write_bytes(encrypted.ciphertext)
        decks = json.dumps(
            [
                {"id": 1, "name": "Default"},
                {"id": 2, "name": "Papers"},
                {"id": 3, "name": "Papers::Advanced"},
            ],
            ensure_ascii=False,
        )
        database.execute(
            """INSERT INTO anki_accounts
               (user_id, available_decks_json, mirror_path, mirror_nonce,
                mirror_key_version, state, selected_deck_id, selected_deck_name,
                updated_at)
               VALUES (?, ?, ?, ?, ?, 'connected', 2, 'Papers', '2026-01-01')""",
            (
                user_id,
                decks,
                str(mirror_path),
                encrypted.nonce,
                encrypted.key_version,
            ),
        )
        database.commit()


def _add_highlight(client, document_id: str, target: str = "robust", sentence: str = "This is a robust result.") -> None:
    reader = client.get(f"/article/{document_id}")
    response = client.post(
        f"/api/article/{document_id}/highlights",
        json={
            "id": str(uuid.uuid4()),
            "target": target,
            "sentence": sentence,
            "page": 1,
            "rects": [{"x1": 80, "y1": 190, "x2": 120, "y2": 205}],
        },
        headers={"X-CSRF-Token": csrf(reader)},
    )
    assert response.status_code == 200


def _site_card_id(tmp_path: Path) -> str:
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        return str(
            database.execute(
                "SELECT id FROM cards WHERE semantic_version = 1 ORDER BY created_at"
            ).fetchone()[0]
        )


def _read_rebuilt(tmp_path: Path, response, collection_name: str = "collection.anki2") -> sqlite3.Connection:
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    assert "media" in archive.namelist()
    path = tmp_path / "rebuilt.sqlite"
    path.write_bytes(archive.read(collection_name))
    return sqlite3.connect(path)


def _start_rebuild(client, tmp_path: Path, **extra) -> None:
    response = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(client.get("/settings")), **extra},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/settings#rebuild"


def _rebuild_download(client) -> tuple[dict, Any]:
    status = client.get("/api/rebuild/status")
    assert status.status_code == 200
    payload = status.get_json()["rebuild_job"]
    assert payload is not None
    assert payload["state"] == "succeeded"
    assert payload["progress"] == 100
    assert payload["download_url"]
    download = client.get(payload["download_url"])
    assert download.status_code == 200
    assert download.headers["Content-Disposition"].startswith(
        "attachment; filename=anki-papers-rebuild-"
    )
    import re

    stamp = re.search(
        r"filename=anki-papers-rebuild-(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.apkg",
        download.headers["Content-Disposition"],
    )
    assert stamp, "download filename must carry the build timestamp"
    assert payload["created_at"].startswith(stamp.group(1)[:10] + "T")
    return payload, download


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
        assert _REBUILD_DECK_NAME_RE.match(deck["1"]["name"])
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
        rebuild_module,
        "_deck_name",
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


def test_rebuild_reuses_llm_cache_after_card_removal(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    original = webapp_module.analyse_cluster_assignment
    calls: list[tuple] = []

    def counting_cluster(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(webapp_module, "analyse_cluster_assignment", counting_cluster)
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
    assert len(calls) == 1

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute("DELETE FROM card_highlights")
        database.execute("DELETE FROM cards")
        database.commit()

    response = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(client.get("/settings"))},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert len(calls) == 1

    second_response = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(client.get("/settings"))},
        content_type="multipart/form-data",
    )
    assert second_response.status_code == 302
    assert len(calls) == 1


def test_cluster_analysis_cached_skips_repeated_calls(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    calls = {"count": 0}

    def fake(target, normalized_target, sentence, candidates, **kwargs):
        calls["count"] += 1
        return ClusterAnalysis(
            cluster_id="new_cluster",
            leader="robust",
            part_of_speech="adjective",
            cluster_definition_en="reliable and resilient in operation",
            translations_ru=["надёжный", "устойчивый"],
            replacement_ru="надёжный",
            source_distractors={
                "substitutes_en": ["strong", "durable"],
                "related_en": ["strength", "resilience"],
                "valid_substitutes_en": ["strong"],
                "valid_related_en": ["strength"],
            },
        )

    monkeypatch.setattr(webapp_module, "analyse_cluster_assignment", fake)
    cache_dir = tmp_path / "caches"
    kwargs = {
        "model": "model-x",
        "target": "robust",
        "normalized_target": "robust",
        "sentence": "The design proved robust in tests.",
        "candidates": [],
        "api_key": "key",
    }
    analysis, was_cached = webapp_module.cluster_analysis_cached(
        cache_dir, 7, **kwargs
    )
    assert not was_cached
    assert analysis.leader == "robust"
    second, was_cached = webapp_module.cluster_analysis_cached(cache_dir, 7, **kwargs)
    assert was_cached
    assert second == analysis
    assert calls["count"] == 1


def test_context_approval_cached_skips_repeated_calls(tmp_path: Path, monkeypatch) -> None:
    calls = {"count": 0}

    def fake(leader, definition, translations, known_contexts, candidates, **kwargs):
        calls["count"] += 1
        assert leader == "robust"
        return {item.id for item in candidates}

    monkeypatch.setattr(webapp_module, "approve_context_candidates", fake)
    cache_dir = tmp_path / "caches"
    kwargs = {
        "model": "model-x",
        "leader": "robust",
        "definition": "definition",
        "translations": ["надёжный"],
        "known_contexts": ["known sentence"],
        "candidates": [
            webapp_module.ContextCandidate(id="a", surface="robust", sentence="One sentence."),
            webapp_module.ContextCandidate(id="b", surface="robust", sentence="Another sentence."),
        ],
        "api_key": "key",
    }
    first = webapp_module.context_approval_cached(cache_dir, 7, **kwargs)
    second = webapp_module.context_approval_cached(cache_dir, 7, **kwargs)
    assert first == {"a", "b"}
    assert second == {"a", "b"}
    assert calls["count"] == 1


def test_rebuild_apkg_imports_in_anki_with_schedule(tmp_path: Path, monkeypatch) -> None:
    if os.environ.get("RUN_ANKI_SYNC_INTEGRATION") != "1":
        pytest.skip("set RUN_ANKI_SYNC_INTEGRATION=1")
    pytest.importorskip("anki")
    from anki.collection import Collection
    from anki.import_export_pb2 import (
        ImportAnkiPackageOptions,
        ImportAnkiPackageRequest,
    )

    install_fake_enrichment(monkeypatch)
    app = make_app(tmp_path)
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

    full_old = tmp_path / "full-old.apkg"
    write_apkg(
        full_old,
        "collection.anki2",
        _full_legacy_collection_bytes(tmp_path, site_id),
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.row_factory = sqlite3.Row
        user_id = database.execute("SELECT id FROM users").fetchone()["id"]
        content = rebuild_module.build_rebuilt_deck_apkg(
            database,
            user_id,
            data_dir=tmp_path,
            source_path=full_old,
        )
    apkg = tmp_path / "rebuilt.apkg"
    apkg.write_bytes(content)

    destination = tmp_path / "imported.anki2"
    collection = Collection(str(destination))
    try:
        collection.import_anki_package(
            ImportAnkiPackageRequest(
                package_path=str(apkg.resolve()),
                options=ImportAnkiPackageOptions(
                    merge_notetypes=True,
                    with_scheduling=True,
                    with_deck_configs=False,
                ),
            )
        )
        assert any(
            _REBUILD_DECK_NAME_RE.match(deck.name)
            for deck in collection.decks.all_names_and_ids()
        )
        cards = [collection.get_card(card_id) for card_id in collection.find_cards("")]
        assert len(cards) == 2
        by_state = {
            (card.type, card.queue): card for card in cards
        }
        meaning = by_state[(2, 2)]
        recall = by_state[(3, 1)]
        assert meaning.ivl == 25 and meaning.reps == 6 and meaning.lapses == 1
        assert recall.ivl == 7 and recall.reps == 3 and recall.lapses == 0
        assert abs(meaning.due - 3333) <= 31
        assert recall.due == 4444
    finally:
        collection.close()


def test_rebuild_second_deck_imports_beside_old_one(tmp_path: Path, monkeypatch) -> None:
    """A fresh rebuild must import as a new deck without merging into old cards.

    Note GUIDs are the import dedup key in Anki: content-only GUIDs make a
    second rebuild update existing notes in place, silently leaving the fresh
    deck empty. GUIDs scoped to the rebuild's deck name keep every rebuild a
    self-contained deck, and the schedules of already-imported cards stay
    untouched.
    """
    if os.environ.get("RUN_ANKI_SYNC_INTEGRATION") != "1":
        pytest.skip("set RUN_ANKI_SYNC_INTEGRATION=1")
    pytest.importorskip("anki")
    from anki.collection import Collection
    from anki.import_export_pb2 import (
        ImportAnkiPackageOptions,
        ImportAnkiPackageRequest,
    )

    install_fake_enrichment(monkeypatch)
    app = make_app(tmp_path)
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

    full_old = tmp_path / "full-old.apkg"
    write_apkg(
        full_old,
        "collection.anki2",
        _full_legacy_collection_bytes(tmp_path, site_id),
    )

    def build_deck(deck_name: str) -> Path:
        monkeypatch.setattr(
            rebuild_module, "_deck_name", lambda _value: deck_name
        )
        with sqlite3.connect(tmp_path / "app.sqlite3") as database:
            database.row_factory = sqlite3.Row
            user_id = database.execute("SELECT id FROM users").fetchone()["id"]
            content = rebuild_module.build_rebuilt_deck_apkg(
                database,
                user_id,
                data_dir=tmp_path,
                source_path=full_old,
            )
        path = tmp_path / f"{deck_name[-5:]}.apkg"
        path.write_bytes(content)
        return path

    first = build_deck("Anki Papers (пересборка) 2026-01-01 10:00")
    second = build_deck("Anki Papers (пересборка) 2026-01-01 11:00")

    destination = tmp_path / "imported-twice.anki2"
    collection = Collection(str(destination))
    try:
        for package in (first, second):
            collection.import_anki_package(
                ImportAnkiPackageRequest(
                    package_path=str(package.resolve()),
                    options=ImportAnkiPackageOptions(
                        merge_notetypes=True,
                        with_scheduling=True,
                        with_deck_configs=False,
                    ),
                )
            )
        deck_names = sorted(
            str(deck.name)
            for deck in collection.decks.all_names_and_ids()
            if "пересборка" in deck.name
        )
        assert deck_names == [
            "Anki Papers (пересборка) 2026-01-01 10:00",
            "Anki Papers (пересборка) 2026-01-01 11:00",
        ]
        deck_ids = {
            str(deck.name): deck.id
            for deck in collection.decks.all_names_and_ids()
            if "пересборка" in deck.name
        }
        cards = [collection.get_card(card_id) for card_id in collection.find_cards("")]
        assert len(cards) == 4
        for card in cards:
            deck = collection.decks.get(card.did)
            assert _REBUILD_DECK_NAME_RE.match(deck["name"])
        by_deck = {
            deck_ids["Anki Papers (пересборка) 2026-01-01 10:00"]: set(),
            deck_ids["Anki Papers (пересборка) 2026-01-01 11:00"]: set(),
        }
        for card in cards:
            by_deck[card.did].add((card.type, card.ivl, card.reps))
        assert by_deck[deck_ids["Anki Papers (пересборка) 2026-01-01 10:00"]] == {
            (2, 25, 6),
            (3, 7, 3),
        }
        assert by_deck[deck_ids["Anki Papers (пересборка) 2026-01-01 11:00"]] == {
            (2, 25, 6),
            (3, 7, 3),
        }
    finally:
        collection.close()


def test_rebuild_guid_is_scoped_to_deck_name() -> None:
    front = '<div class="anki-papers-semantic" data-key="card-1">front</div>'
    first = rebuild_module._rebuild_guid(front, "Anki Papers (пересборка) 2026-01-01 10:00")
    second = rebuild_module._rebuild_guid(front, "Anki Papers (пересборка) 2026-01-01 11:00")
    replay = rebuild_module._rebuild_guid(front, "Anki Papers (пересборка) 2026-01-01 10:00")
    assert len(first) <= 12
    assert first != second
    assert first == replay


def test_cluster_candidate_models_builds_valid_objects() -> None:
    clusters = [
        rebuild_module.RebuildCluster(
            id="c1",
            target="robust",
            leader="robust",
            sentence="A robust result.",
            part_of_speech="adjective",
            sense_definition_en="definition",
            translations=["надёжный"],
            contexts=[
                {"target": "Robust", "sentence": "  A robust result.  "},
                {"target": "", "sentence": "No target here."},
            ],
            highlight_ids=["h1", "h2"],
            old_card_ids=[],
            source_page=1,
            document_id="a",
        ),
        rebuild_module.RebuildCluster(
            id="c2",
            target="robust",
            leader="",
            sentence="Orphan cluster.",
            part_of_speech="adjective",
            sense_definition_en="",
            translations=[],
            contexts=[{"target": "robust", "sentence": "Orphan context."}],
            highlight_ids=["h3"],
            old_card_ids=[],
            source_page=1,
            document_id="b",
        ),
        rebuild_module.RebuildCluster(
            id="c3",
            target="robust",
            leader="orphan",
            sentence="Empty contexts.",
            part_of_speech="adjective",
            sense_definition_en="",
            translations=[],
            contexts=[{"target": "", "sentence": ""}],
            highlight_ids=["h4"],
            old_card_ids=[],
            source_page=1,
            document_id="c",
        ),
    ]
    candidates = rebuild_module._cluster_candidate_models(clusters)
    assert [candidate.cluster_id for candidate in candidates] == ["c1"]
    candidate = candidates[0]
    assert isinstance(candidate, ClusterCandidate)
    assert candidate.leader == "robust"
    assert [example.highlight for example in candidate.examples] == ["robust"]
    assert candidate.examples[0].context == "A robust result."


def test_collect_seeds_accepts_all_highlight_sources(tmp_path: Path) -> None:
    database = sqlite3.connect(tmp_path / "seeds.sqlite")
    try:
        database.executescript(
            """
            CREATE TABLE highlights (id TEXT PRIMARY KEY, user_id INTEGER);
            CREATE TABLE cards (
                id TEXT PRIMARY KEY, user_id INTEGER, semantic_version INTEGER,
                contexts_json TEXT
            );
            CREATE TABLE card_highlights (
                card_id TEXT, highlight_id TEXT, PRIMARY KEY(card_id, highlight_id)
            );
            """
        )
        database.execute("INSERT INTO highlights VALUES ('h1', 1), ('h2', 1), ('other', 1)")
        context = {
            "id": "h1",
            "source": "reader",
            "target": "robust",
            "sentence": "A robust result.",
            "lemma": "robust",
            "part_of_speech": "adjective",
            "sense_definition_en": "definition",
            "translations": ["надёжный"],
            "replacement": "надёжный",
            "substitutes_en": [],
            "related_en": [],
            "valid_substitutes_en": [],
            "valid_related_en": [],
        }
        pdf_context = {
            "id": "h2",
            "source": "pdf_import",
            "target": "result",
            "sentence": "A final result.",
            "lemma": "result",
            "part_of_speech": "noun",
            "sense_definition_en": "outcome",
            "translations": ["итог"],
            "replacement": "итог",
            "substitutes_en": [],
            "related_en": [],
            "valid_substitutes_en": [],
            "valid_related_en": [],
        }
        article_context = {
            "id": "article:1:ctx-0001",
            "source": "article_context",
            "target": "robust",
            "sentence": "A robust result.",
            "lemma": "robust",
            "part_of_speech": "adjective",
            "sense_definition_en": "definition",
            "translations": ["надёжный"],
            "replacement": "надёжный",
            "substitutes_en": [],
            "related_en": [],
            "valid_substitutes_en": [],
            "valid_related_en": [],
        }
        database.execute(
            "INSERT INTO card_highlights VALUES ('card1', 'h1'), ('card1', 'h2')"
        )
        database.execute(
            "INSERT INTO cards VALUES ('card1', 1, 1, ?)",
            (json.dumps([context, pdf_context, article_context]),),
        )
        database.execute("INSERT INTO cards VALUES ('card2', 1, 1, ?)", ("[]",))
        database.execute("INSERT INTO cards VALUES ('card3', 1, 0, ?)", ("[]",))
        database.row_factory = sqlite3.Row
        seeds, seeds_old_cards = rebuild_module._collect_seeds(database, 1)
    finally:
        database.close()
    assert set(seeds) == {"h1", "h2"}
    assert seeds["h1"]["cluster_id"] == "new_cluster"
    assert seeds["h1"]["leader"] == "robust"
    assert seeds_old_cards == {"h1": "card1", "h2": "card1"}


def test_deck_name_carries_minute_timestamp() -> None:
    name = rebuild_module._deck_name(datetime(2026, 8, 18, 22, 50, 37, tzinfo=UTC))
    assert name == "Anki Papers (пересборка) 2026-08-18 22:50"


def test_rebuild_merges_clusters_with_colliding_ids(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)

    def constant_leader(target, normalized_target, sentence, candidates, **_kwargs):
        return ClusterAnalysis(
            cluster_id=candidates[0].cluster_id if candidates else "new_cluster",
            leader="robust",
            part_of_speech="adjective",
            cluster_definition_en="reliable and resilient in operation",
            translations_ru=["надёжный", "устойчивый"],
            replacement_ru="надёжный",
            source_distractors={
                "substitutes_en": ["strong", "durable"],
                "related_en": ["strength", "resilience"],
                "valid_substitutes_en": ["strong"],
                "valid_related_en": ["strength"],
            },
        )

    monkeypatch.setattr(webapp_module, "analyse_cluster_assignment", constant_leader)
    app = make_app(tmp_path, REBUILD_INLINE=True)
    client = app.test_client()
    response = identify(client)

    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    sentence = "This is a robust result."
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
        document_id = database.execute(
            "SELECT id FROM documents WHERE kind = 'pdf' ORDER BY created_at LIMIT 1"
        ).fetchone()[0]
        for index, target in enumerate(("robust", "qqq")):
            database.execute(
                """INSERT INTO highlights
                   (id, user_id, document_id, target, sentence, page, rects_json,
                    translations_json, replacement, alternatives_json, status,
                    error, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '', '[]', 'ready', NULL,
                           'reader', ?, ?)""",
                (
                    str(uuid.uuid4()),
                    user_id,
                    document_id,
                    target,
                    sentence,
                    1,
                    f"[{index}]",
                    f"2026-08-18T1{index}:00:00",
                    f"2026-08-18T1{index}:00:00",
                ),
            )
        database.commit()

    _start_rebuild(client, tmp_path)
    _payload, download = _rebuild_download(client)
    database = _read_rebuilt(tmp_path, download)
    try:
        note_ids = [row[0] for row in database.execute("SELECT id FROM notes")]
        assert len(note_ids) == len(set(note_ids)) == 2
    finally:
        database.close()


def test_settings_shows_running_rebuild_job_panel(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    app = make_app(tmp_path)
    client = app.test_client()
    identify(client)
    timestamp = datetime.now(UTC).isoformat()
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
        database.execute(
            """INSERT INTO rebuild_jobs
               (id, user_id, state, deck_id, progress, stage, error, result_path,
                created_at, started_at, finished_at, updated_at)
               VALUES ('job-1', ?, 'running', NULL, 35, 'Контексты предложений', '',
                       NULL, ?, ?, NULL, ?)""",
            (user_id, timestamp, timestamp, timestamp),
        )
        database.commit()

    page = client.get("/settings")
    assert page.status_code == 200
    assert 'data-rebuild-job' in page.text
    assert 'data-job-id="job-1"' in page.text
    assert 'data-state="running"' in page.text
    assert 'data-progress="35"' in page.text
    assert 'src="{{ url_for(' not in page.text
    assert "rebuild-status.js" in page.text
    assert "disabled" in page.text

    status = client.get("/api/rebuild/status")
    payload = status.get_json()["rebuild_job"]
    assert payload["state"] == "running"
    assert payload["progress"] == 35
    assert payload["stage"] == "Контексты предложений"
    assert payload["download_url"] is None


def test_rebuild_job_runs_in_background_and_survives_page_reload(
    tmp_path: Path, monkeypatch
) -> None:
    install_fake_enrichment(monkeypatch)
    monkeypatch.setattr(
        rebuild_module,
        "_deck_name",
        lambda value: "Anki Papers (пересборка) 2026-01-01 00:00",
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

    _start_rebuild(client, tmp_path)

    deadline = time.monotonic() + 30
    payload: dict | None = None
    while time.monotonic() < deadline:
        status = client.get("/api/rebuild/status")
        payload = status.get_json()["rebuild_job"]
        assert payload is not None
        state = payload["state"]
        assert state in {"queued", "running", "succeeded"}
        if state == "succeeded":
            break
        time.sleep(0.1)
    assert payload is not None and payload["state"] == "succeeded"
    assert payload["progress"] == 100
    assert 0 <= payload["progress"] <= 100

    reloaded = client.get("/settings")
    assert "data-state=\"succeeded\"" in reloaded.text
    assert 'data-download-url="' in reloaded.text

    download = client.get(payload["download_url"])
    assert download.status_code == 200
    assert download.data.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
        assert len(archive.namelist()) >= 2


def test_cluster_analysis_cached_coerces_dict_candidates(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    calls: dict[str, int] = {"count": 0}

    def collecting_cluster(*args, **kwargs):
        calls["count"] += 1
        candidates = kwargs.get("candidates") if "candidates" in kwargs else args[3]
        assert all(isinstance(candidate, ClusterCandidate) for candidate in candidates)
        return ClusterAnalysis(
            cluster_id=str(candidates[0].cluster_id),
            leader=str(candidates[0].leader),
            part_of_speech="adjective",
            cluster_definition_en="definition",
            translations_ru=["надёжный"],
            replacement_ru="надёжный",
            source_distractors=RecallDistractors(
                substitutes_en=[],
                related_en=[],
                valid_substitutes_en=[],
                valid_related_en=[],
            ),
        )

    monkeypatch.setattr(webapp_module, "analyse_cluster_assignment", collecting_cluster)
    kwargs = {
        "cache_dir": tmp_path / "caches",
        "user_id": 7,
        "model": "model",
        "target": "robust",
        "normalized_target": "robust",
        "sentence": "A robust result.",
        "candidates": [
            {"cluster_id": "c1", "leader": "robust", "examples": [
                {"highlight": "robust", "context": "A robust result."}
            ]}
        ],
        "api_key": "key",
    }
    first, first_cached = webapp_module.cluster_analysis_cached(**kwargs)
    second, second_cached = webapp_module.cluster_analysis_cached(**kwargs)
    assert first.cluster_id == "c1" and not first_cached
    assert second.cluster_id == "c1" and second_cached
    assert calls["count"] == 1


def test_rebuild_accepts_modern_unicase_collection(tmp_path: Path, monkeypatch) -> None:
    if os.environ.get("RUN_ANKI_SYNC_INTEGRATION") != "1":
        pytest.skip("set RUN_ANKI_SYNC_INTEGRATION=1")
    pytest.importorskip("anki")
    from anki.collection import Collection
    from anki.import_export_pb2 import ExportAnkiPackageOptions, ExportLimit

    install_fake_enrichment(monkeypatch)
    app = make_app(tmp_path)
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

    modern = tmp_path / "modern.anki2"
    collection = Collection(str(modern))
    note_type = collection.models.by_name("Basic")
    for direction, (kind, queue, due, ivl, reps, lapses) in {
        "meaning": (2, 2, 3333, 25, 6, 1),
        "recall": (3, 1, 4444, 7, 3, 0),
    }.items():
        note = collection.new_note(note_type)
        note["Front"] = f"old {direction}"
        note["Back"] = "back"
        note.tags = [f"anki_papers::{site_id}", "semantic::v1", f"card::{direction}"]
        collection.add_note(note, collection.decks.id("Default"))
        card = note.cards()[0]
        card.type = kind
        card.queue = queue
        card.due = due
        card.ivl = ivl
        card.factor = 2500
        card.reps = reps
        card.lapses = lapses
        collection.update_card(card)
    modern_apkg = tmp_path / "modern-old.apkg"
    collection.export_anki_package(
        out_path=str(modern_apkg),
        options=ExportAnkiPackageOptions(with_scheduling=True, legacy=False),
        limit=ExportLimit(deck_id=1),
    )
    collection.close()

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.row_factory = sqlite3.Row
        user_id = database.execute("SELECT id FROM users").fetchone()["id"]
        content = rebuild_module.build_rebuilt_deck_apkg(
            database,
            user_id,
            data_dir=tmp_path,
            source_path=modern_apkg,
        )
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert "collection.anki21b" in archive.namelist()
        rebuilt_bytes = archive.read("collection.anki21b")
    # Unwrap the zstd collection so a plain sqlite3 reader can inspect it.
    zstandard = pytest.importorskip("zstandard")
    modern_path = tmp_path / "rebuilt-modern.sqlite"
    with io.BytesIO(rebuilt_bytes) as raw_stream, modern_path.open("wb") as output:
        zstandard.ZstdDecompressor().copy_stream(raw_stream, output)
    database = sqlite3.connect(modern_path)
    try:
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 2
        rows = database.execute(
            "SELECT n.tags, c.type, c.queue, c.due, c.ivl, c.factor, c.reps, c.lapses "
            "FROM notes n JOIN cards c ON c.nid = n.id"
        ).fetchall()
        by_direction = {
            "meaning" if "card::meaning" in row[0] else "recall": row
            for row in rows
        }
        assert by_direction["meaning"][1:] == (2, 2, 3333, 25, 2500, 6, 1)
        assert by_direction["recall"][1:] == (3, 1, 4444, 7, 2500, 3, 0)
        assert _REBUILD_DECK_NAME_RE.match(
            database.execute("SELECT name FROM decks WHERE id = 1").fetchone()[0]
        )
    finally:
        database.close()
