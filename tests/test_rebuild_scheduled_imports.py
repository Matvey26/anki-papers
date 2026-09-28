from __future__ import annotations

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
        with sqlite3.connect(":memory:") as source, zipfile.ZipFile(full_old) as package:
            source.deserialize(package.read("collection.anki2"))
            source_crt = source.execute("SELECT crt FROM col").fetchone()[0]
        # Anki shifts day-based due values between collection creation dates.
        source_due_at = source_crt + 3333 * 86400
        imported_due_at = collection.crt + meaning.due * 86400
        assert abs(imported_due_at - source_due_at) < 86400
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
            ref_rebuild, "_deck_name", lambda _value: deck_name
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
