from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import articles_to_anki.rebuilding.collection as ref_rebuilding_collection
import articles_to_anki.rebuilding.identity as ref_rebuilding_identity


def test_rebuild_guid_is_scoped_to_deck_name() -> None:
    front = '<div class="anki-papers-semantic" data-key="card-1">front</div>'
    first = ref_rebuilding_identity._rebuild_guid(front, "Anki Papers (пересборка) 2026-01-01 10:00")
    second = ref_rebuilding_identity._rebuild_guid(front, "Anki Papers (пересборка) 2026-01-01 11:00")
    replay = ref_rebuilding_identity._rebuild_guid(front, "Anki Papers (пересборка) 2026-01-01 10:00")
    assert len(first) <= 12
    assert first != second
    assert first == replay


def test_avoid_default_deck_id_moves_single_legacy_deck(tmp_path: Path) -> None:
    """Deck id 1 is skipped by Anki's apkg importer when scheduling is not
    included; the rebuild deck must be re-id'd off the default id."""
    database = sqlite3.connect(tmp_path / "legacy.sqlite")
    database.executescript(
        "CREATE TABLE col (id INTEGER PRIMARY KEY, decks TEXT NOT NULL)"
    )
    decks = {
        "1": {"name": "По умолчанию", "id": 1, "conf": 1, "mod": 0, "usn": 0},
        "5": {"name": "Papers", "id": 5, "conf": 1, "mod": 0, "usn": 0},
    }
    database.execute("INSERT INTO col VALUES (1, ?)", (json.dumps(decks),))
    rebuilt = ref_rebuilding_collection._avoid_default_deck_id(database, 1)
    assert rebuilt == 6
    stored = json.loads(database.execute("SELECT decks FROM col").fetchone()[0])
    assert set(stored) == {"6", "5"}
    assert stored["6"]["name"] == "По умолчанию"
    assert stored["6"]["id"] == 6


def test_avoid_default_deck_id_relocates_modern_deck_row(tmp_path: Path) -> None:
    database = sqlite3.connect(tmp_path / "modern.sqlite")
    database.executescript(
        "CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
    )
    database.executescript(
        "INSERT INTO decks (id, name) VALUES (1, 'По умолчанию'), (5, 'Papers')"
    )
    rebuilt = ref_rebuilding_collection._avoid_default_deck_id(database, 1)
    assert rebuilt == 6
    rows = dict(database.execute("SELECT id, name FROM decks").fetchall())
    assert rows == {6: "По умолчанию", 5: "Papers"}


def test_avoid_default_deck_id_keeps_non_default(tmp_path: Path) -> None:
    database = sqlite3.connect(tmp_path / "other.sqlite")
    database.executescript(
        "CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
    )
    database.execute("INSERT INTO decks (id, name) VALUES (5, 'Papers')")
    assert ref_rebuilding_collection._avoid_default_deck_id(database, 5) == 5
    assert dict(database.execute("SELECT id, name FROM decks").fetchall()) == {
        5: "Papers"
    }


def test_deck_name_carries_minute_timestamp() -> None:
    name = ref_rebuilding_identity._deck_name(datetime(2026, 8, 18, 22, 50, 37, tzinfo=UTC))
    assert name == "Anki Papers (пересборка) 2026-08-18 22:50"
