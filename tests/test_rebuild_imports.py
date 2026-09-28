from __future__ import annotations

import io
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest
from support_rebuild import (
    _REBUILD_DECK_NAME_RE,
    _add_highlight,
    _full_legacy_collection_bytes,
    _site_card_id,
)
from support_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes
from test_apkg import write_apkg

import articles_to_anki.rebuild as ref_rebuild


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
        content = ref_rebuild.build_rebuilt_deck_apkg(
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
        rebuilt_deck = database.execute(
            "SELECT id, name FROM decks ORDER BY id LIMIT 1"
        ).fetchone()
        assert _REBUILD_DECK_NAME_RE.match(rebuilt_deck[1])
        # The built deck must not reuse Anki's default id (1): the apkg
        # importer drops deck id 1 when scheduling is not included, silently
        # dumping the cards into the target collection's default deck.
        assert rebuilt_deck[0] != 1
    finally:
        database.close()


def test_rebuild_apkg_imports_in_anki_without_scheduling(
    tmp_path: Path, monkeypatch
) -> None:
    """A rebuild imported WITHOUT scheduling (the desktop default) must still
    create its own deck: Anki's importer drops deck id 1 from packages when
    scheduling is not included, silently dumping cards into Default."""
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
        document_id = database.execute(
            "SELECT id FROM documents WHERE kind = 'pdf'"
        ).fetchone()[0]
    client.post(
        f"/article/{document_id}/read",
        data={"csrf_token": csrf(response), "read": "1"},
    )
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
        content = ref_rebuild.build_rebuilt_deck_apkg(
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
                    with_scheduling=False,
                    with_deck_configs=False,
                ),
            )
        )
        rebuilt_named = [
            deck
            for deck in collection.decks.all_names_and_ids()
            if _REBUILD_DECK_NAME_RE.match(deck.name)
        ]
        assert rebuilt_named, "rebuild deck was not created"
        assert len(rebuilt_named) == 1
        deck_id = rebuilt_named[0].id
        cards = [collection.get_card(card_id) for card_id in collection.find_cards("")]
        assert len(cards) == 2
        assert all(card.did == deck_id for card in cards)
    finally:
        collection.close()
