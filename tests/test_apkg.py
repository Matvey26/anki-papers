from __future__ import annotations

import csv
import io
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


def write_apkg(
    path: Path,
    collection_name: str,
    content: bytes,
    *,
    write_content_size: bool = True,
) -> None:
    if collection_name.endswith("21b"):
        content = zstandard.ZstdCompressor(
            write_content_size=write_content_size
        ).compress(content)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(collection_name, content)
        archive.writestr("media", "{}")


def read_collection(path: Path, collection_name: str, tmp_path: Path) -> sqlite3.Connection:
    with zipfile.ZipFile(path) as archive:
        content = archive.read(collection_name)
    if collection_name.endswith("21b"):
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(content)) as stream:
            content = stream.read()
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


def test_merge_supports_zstd_without_content_size(tmp_path: Path) -> None:
    source = tmp_path / "source.apkg"
    write_apkg(
        source,
        "collection.anki21b",
        collection_bytes(tmp_path / "source.sqlite"),
        write_content_size=False,
    )
    cards = tmp_path / "new.csv"
    with cards.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Front", "Back", "Tags"])
        writer.writeheader()
        writer.writerow({"Front": "New front", "Back": "new back", "Tags": "new"})

    destination = tmp_path / "destination.apkg"
    merge(source, destination, [cards], tmp_path / "combined.csv")

    database = read_collection(destination, "collection.anki21b", tmp_path)
    assert database.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2
    assert database.execute("SELECT due FROM cards WHERE id = 1").fetchone()[0] == 42
    database.close()


def test_merge_deduplicates_generated_cards_by_type_and_target(tmp_path: Path) -> None:
    source = tmp_path / "source.apkg"
    write_apkg(
        source,
        "collection.anki21b",
        collection_bytes(tmp_path / "source.sqlite"),
    )
    first_csv = tmp_path / "first.csv"
    with first_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Front", "Back", "Tags"])
        writer.writeheader()
        writer.writerow(
            {
                "Front": "A <b>robust</b> result.",
                "Back": "• надёжный",
                "Tags": "article::paper card::meaning",
            }
        )
        writer.writerow(
            {
                "Front": "A <b>надёжный</b> result.",
                "Back": "<b>robust</b>",
                "Tags": "article::paper card::recall",
            }
        )
    first = tmp_path / "first.apkg"
    merge(source, first, [first_csv], tmp_path / "first-combined.csv")

    second_csv = tmp_path / "second.csv"
    with second_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Front", "Back", "Tags"])
        writer.writeheader()
        writer.writerow(
            {
                "Front": "A different <b>robust</b> example.",
                "Back": "• устойчивый",
                "Tags": "article::paper card::meaning",
            }
        )
        writer.writerow(
            {
                "Front": "A different <b>устойчивый</b> example.",
                "Back": "<b>robust</b>",
                "Tags": "article::paper card::recall",
            }
        )
        writer.writerow(
            {
                "Front": "A <b>precise</b> result.",
                "Back": "• точный",
                "Tags": "article::paper card::meaning",
            }
        )
        writer.writerow(
            {
                "Front": "A <b>точный</b> result.",
                "Back": "<b>precise</b>",
                "Tags": "article::paper card::recall",
            }
        )
    destination = tmp_path / "destination.apkg"
    merge(first, destination, [second_csv], tmp_path / "second-combined.csv")

    database = read_collection(destination, "collection.anki21b", tmp_path)
    fields = [row[0] for row in database.execute("SELECT flds FROM notes")]
    assert database.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 5
    assert sum("robust" in value for value in fields) == 2
    assert sum("precise" in value for value in fields) == 2
    assert all("different" not in value.casefold() for value in fields)
    database.close()


def test_merge_removes_existing_duplicates_and_keeps_latest_scheduled_card(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "duplicates.sqlite"
    raw_path.write_bytes(collection_bytes(tmp_path / "base.sqlite"))
    database = sqlite3.connect(raw_path)
    for note_id, due, reps, translation in (
        (2, 12, 2, "старый перевод"),
        (3, 30, 1, "нужный перевод"),
        (4, 200, 0, "новый дубль без расписания"),
    ):
        database.execute(
            "INSERT INTO notes VALUES (?, ?, 100, 1, 0, ' article::paper card::meaning ', ?, ?, 1, 0, '')",
            (
                note_id,
                f"guid-{note_id}",
                f"A <b>robust</b> result.\x1f{translation}",
                "A robust result.",
            ),
        )
        database.execute(
            "INSERT INTO cards VALUES (?, ?, 200, 0, 1, 0, ?, ?, ?, 5, 2500, ?, 0, 0, 0, 0, 0, '{}')",
            (note_id, note_id, 2 if reps else 0, 2 if reps else 0, due, reps),
        )
    database.commit()
    database.close()
    source = tmp_path / "duplicates.apkg"
    write_apkg(source, "collection.anki21b", raw_path.read_bytes())
    empty_csv = tmp_path / "empty.csv"
    with empty_csv.open("w", encoding="utf-8", newline="") as stream:
        csv.DictWriter(stream, fieldnames=["Front", "Back", "Tags"]).writeheader()

    destination = tmp_path / "clean.apkg"
    merge(source, destination, [empty_csv], tmp_path / "combined.csv")

    cleaned = read_collection(destination, "collection.anki21b", tmp_path)
    matching = cleaned.execute(
        """SELECT cards.id, cards.due, cards.reps, notes.flds
           FROM cards JOIN notes ON notes.id = cards.nid
           WHERE notes.tags LIKE '%card::meaning%'"""
    ).fetchall()
    assert matching == [(3, 30, 1, "A <b>robust</b> result.\x1fнужный перевод")]
    assert cleaned.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 2
    cleaned.close()
