from __future__ import annotations

import csv
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
import zstandard

from articles_to_anki.apkg import merge


def collection_bytes(path: Path) -> bytes:
    database = sqlite3.connect(path)
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
    database.execute(
        "INSERT INTO notes VALUES (1, 'old', 100, 1, 0, ' old ', 'Existing front\x1fold back', 'Existing front', 1, 0, '')"
    )
    database.execute(
        "INSERT INTO cards VALUES (1, 1, 200, 0, 1, 0, 2, 2, 42, 10, 2500, 4, 0, 0, 0, 0, 0, '{}')"
    )
    database.commit()
    database.close()
    content = path.read_bytes()
    path.unlink()
    return content


def write_apkg(path: Path, collection_name: str, content: bytes) -> None:
    if collection_name.endswith("21b"):
        content = zstandard.ZstdCompressor().compress(content)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(collection_name, content)
        archive.writestr("media", "{}")


def read_collection(path: Path, collection_name: str, tmp_path: Path) -> sqlite3.Connection:
    with zipfile.ZipFile(path) as archive:
        content = archive.read(collection_name)
    if collection_name.endswith("21b"):
        content = zstandard.ZstdDecompressor().decompress(content)
    database_path = tmp_path / f"read-{collection_name}.sqlite"
    database_path.write_bytes(content)
    return sqlite3.connect(database_path)


@pytest.mark.parametrize("collection_name", ["collection.anki2", "collection.anki21b"])
def test_merge_preserves_schedule_and_skips_duplicate_fronts(tmp_path: Path, collection_name: str) -> None:
    source = tmp_path / "source.apkg"
    write_apkg(source, collection_name, collection_bytes(tmp_path / "source.sqlite"))
    cards = tmp_path / "new.csv"
    with cards.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Front", "Back", "Tags"])
        writer.writeheader()
        writer.writerow({"Front": "Existing front", "Back": "duplicate", "Tags": "new"})
        writer.writerow({"Front": "New front", "Back": "new back", "Tags": "new"})

    destination = tmp_path / "destination.apkg"
    merge(source, destination, [cards], tmp_path / "combined.csv")

    database = read_collection(destination, collection_name, tmp_path)
    assert database.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2
    assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 2
    assert database.execute("SELECT due FROM cards WHERE id = 1").fetchone()[0] == 42
    assert database.execute("SELECT type, queue FROM cards WHERE id != 1").fetchone() == (0, 0)
    database.close()
