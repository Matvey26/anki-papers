from __future__ import annotations

import io
import json
import os
import sqlite3
import time
import uuid
import zipfile
from pathlib import Path

import pytest
from test_apkg import write_apkg
from test_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.webapp as webapp_module
from articles_to_anki.models import ClusterAnalysis


def _legacy_collection_bytes(
    tmp_path: Path,
    notes: list[tuple[str, str, dict[str, int]]],
) -> bytes:
    database = sqlite3.connect(tmp_path / "old.sqlite")
    database.executescript(
        """
        CREATE TABLE col (id INTEGER PRIMARY KEY, mod INTEGER, models TEXT, decks TEXT);
        CREATE TABLE decks (id INTEGER PRIMARY KEY);
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY, guid TEXT, mid INTEGER, mod INTEGER, usn INTEGER,
            tags TEXT, flds TEXT, sfld TEXT, csum INTEGER, flags INTEGER, data TEXT
        );
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER, ord INTEGER, mod INTEGER,
            usn INTEGER, type INTEGER, queue INTEGER, due INTEGER, ivl INTEGER,
            factor INTEGER, reps INTEGER, lapses INTEGER, left INTEGER, odue INTEGER,
            odid INTEGER, flags INTEGER, data TEXT
        );
        """
    )
    models = json.dumps({"100": {"flds": [{"name": "Front"}, {"name": "Back"}]}})
    decks = json.dumps({"200": {"name": "Default"}})
    database.execute("INSERT INTO col VALUES (1, 1, ?, ?)", (models, decks))
    database.execute("INSERT INTO decks VALUES (200)")
    for index, (fields, tags, schedule) in enumerate(notes, start=1):
        database.execute(
            "INSERT INTO notes VALUES (?, 'g', 100, 1, 0, ?, ?, ?, 1, 0, '')",
            (index, tags, fields, fields.partition("\x1f")[0]),
        )
        database.execute(
            "INSERT INTO cards VALUES (?, ?, 200, 0, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '{}')",
            (
                index,
                index,
                schedule["type"],
                schedule["queue"],
                schedule["due"],
                schedule["ivl"],
                schedule["factor"],
                schedule["reps"],
                schedule["lapses"],
                0,
                0,
                0,
            ),
        )
    database.commit()
    database.close()
    content = (tmp_path / "old.sqlite").read_bytes()
    (tmp_path / "old.sqlite").unlink()
    return content


def _old_deck_apkg(tmp_path: Path, site_id: str) -> bytes:
    meaning = "Old meaning front\x1fOld meaning back"
    recall = "Old recall front\x1fOld recall back"
    notes = [
        (
            meaning,
            f" anki_papers::{site_id} semantic::v1 card::meaning ",
            {"type": 2, "queue": 2, "due": 3333, "ivl": 25, "factor": 2500, "reps": 6, "lapses": 1},
        ),
        (
            recall,
            f" anki_papers::{site_id} semantic::v1 card::recall ",
            {"type": 3, "queue": 1, "due": 4444, "ivl": 7, "factor": 2200, "reps": 3, "lapses": 0},
        ),
    ]
    path = tmp_path / "old.apkg"
    write_apkg(path, "collection.anki2", _legacy_collection_bytes(tmp_path, notes))
    return path.read_bytes()


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


def test_rebuild_carries_schedule_from_old_deck(tmp_path: Path, monkeypatch) -> None:
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

    old_deck = _old_deck_apkg(tmp_path, site_id)
    response = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(response), "old_deck": (io.BytesIO(old_deck), "old.apkg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith('attachment; filename="anki-papers-rebuild-')

    database = _read_rebuilt(tmp_path, response)
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
        assert deck["200"]["name"] == "Anki Papers (пересборка)"
        notes = database.execute("SELECT mid, flds FROM notes").fetchall()
        assert all(mid == 100 for mid, _ in notes)
        assert all("\x1f" in fields and fields.strip() for _, fields in notes)
    finally:
        database.close()


def test_rebuild_fresh_without_old_deck_is_deterministic(tmp_path: Path, monkeypatch) -> None:
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
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents WHERE kind = 'pdf'").fetchone()[0]
    client.post(f"/article/{document_id}/read", data={"csrf_token": csrf(response), "read": "1"})
    _add_highlight(client, document_id)

    first = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(client.get("/settings"))},
        content_type="multipart/form-data",
    )
    assert first.status_code == 200
    second = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(client.get("/settings"))},
        content_type="multipart/form-data",
    )
    assert second.status_code == 200
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
    assert response.status_code == 200
    assert len(calls) == 1

    response = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(client.get("/settings"))},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
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
    response = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(response), "old_deck": (io.BytesIO(full_old.read_bytes()), "old.apkg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    apkg = tmp_path / "rebuilt.apkg"
    apkg.write_bytes(response.data)

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
            deck.name == "Anki Papers (пересборка)"
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
