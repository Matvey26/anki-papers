"""rebuilding / collection."""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

import zstandard

from articles_to_anki.rebuilding.constants import (
    _COLLECTION_NAMES,
    _TEMPLATE_JSON,
    _ZSTD_MAGIC,
)


def _avoid_default_deck_id(connection: sqlite3.Connection, deck_id: int) -> int:
    """Rebuild decks must not reuse Anki's default deck id (1).

    The Anki apkg importer skips deck id 1 when scheduling is not included
    (the desktop default), so cards would silently land in the target
    collection's default deck instead of a fresh build deck.
    """
    if deck_id != 1:
        return deck_id
    try:
        row = connection.execute("SELECT decks FROM col LIMIT 1").fetchone()
        decks = json.loads(row[0]) if row else None
    except (sqlite3.OperationalError, TypeError, ValueError, json.JSONDecodeError):
        decks = None
    if decks is not None:
        other = [int(key) for key in decks if int(key) != 1]
        replacement = max(other) + 1 if other else 2
        moved = decks.pop("1")
        moved["id"] = replacement
        decks[str(replacement)] = moved
        connection.execute(
            "UPDATE col SET decks = ?",
            (json.dumps(decks, ensure_ascii=False),),
        )
        return replacement
    row = connection.execute("SELECT MAX(id) FROM decks").fetchone()
    replacement = max(int(row[0]) + 1 if row and row[0] else 0, 2)
    connection.execute("UPDATE decks SET id = ? WHERE id = 1", (replacement,))
    return replacement


def _rename_deck(
    connection: sqlite3.Connection, deck_id: int, deck_name: str
) -> None:
    try:
        row = connection.execute("SELECT decks FROM col LIMIT 1").fetchone()
        decks = json.loads(row[0]) if row else None
    except (sqlite3.OperationalError, TypeError, ValueError, json.JSONDecodeError):
        decks = None
    if decks:
        decks[str(deck_id)]["name"] = deck_name
        connection.execute(
            "UPDATE col SET decks = ?",
            (json.dumps(decks, ensure_ascii=False),),
        )
        return
    try:
        connection.execute(
            "UPDATE decks SET name = ? WHERE id = ?", (deck_name, deck_id)
        )
    except sqlite3.OperationalError:
        raise RuntimeError("Rebuild source contains no decks")


def _rebuild_note_type_id(connection: sqlite3.Connection) -> int:
    try:
        models = json.loads(
            connection.execute("SELECT models FROM col LIMIT 1").fetchone()[0]
        )
    except (sqlite3.OperationalError, TypeError, ValueError, json.JSONDecodeError):
        models = None
    if models:
        for model_id, model in models.items():
            if len(model.get("flds", [])) == 2:
                return int(model_id)
    try:
        counts: dict[int, int] = {}
        for (ntid,) in connection.execute("SELECT ntid FROM fields"):
            counts[int(ntid)] = counts.get(int(ntid), 0) + 1
        for ntid in sorted(counts):
            if counts[ntid] == 2:
                return ntid
    except sqlite3.OperationalError:
        pass
    try:
        row = connection.execute(
            "SELECT mid FROM notes GROUP BY mid ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            return int(row[0])
    except sqlite3.OperationalError:
        pass
    raise RuntimeError("Rebuild source contains no compatible note type")


def _empty_collection(connection: sqlite3.Connection) -> None:
    for table in ("notes", "cards", "revlog", "graves"):
        try:
            connection.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            continue


def _connect_collection(path: Path) -> sqlite3.Connection:
    """Open a collection file, registering the `unicase` collation.

    Modern (Anki 2.1.50+) collections sort deck names and tags with the
    `unicase` collation; without it plain sqlite3 refuses to read those
    tables at all.
    """
    connection = sqlite3.connect(path)

    def unicase(left: str, right: str) -> int:
        normalized_left = unicodedata.normalize("NFC", left or "").casefold()
        normalized_right = unicodedata.normalize("NFC", right or "").casefold()
        return (normalized_left > normalized_right) - (
            normalized_left < normalized_right
        )

    connection.create_collation("unicase", unicase)
    return connection


def _fallback_collection(temporary: Path) -> Path:
    """Build a minimal legacy Anki collection when no old deck is available."""
    payload = json.loads(_TEMPLATE_JSON.read_text(encoding="utf-8"))
    collection = temporary / "collection.anki2"
    database = _connect_collection(collection)
    try:
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
        mod = int(time.time())
        database.execute(
            "INSERT INTO col VALUES (1, ?, ?, ?, ?, 0, -1, 0, ?, ?, ?, ?, ?)",
            (
                payload["crt"],
                mod,
                mod * 1000,
                payload["ver"],
                json.dumps(payload["conf"], ensure_ascii=False),
                json.dumps(payload["models"], ensure_ascii=False),
                json.dumps(payload["decks"], ensure_ascii=False),
                json.dumps(payload["dconf"], ensure_ascii=False),
                json.dumps(payload["tags"], ensure_ascii=False),
            ),
        )
        database.commit()
    finally:
        database.close()
    return collection


def _open_old_collection(
    source_path: Path | None,
    temporary: Path,
) -> tuple[Path, bool]:
    """Return a plain sqlite path for the old deck and whether it was zstd."""
    if source_path is None:
        return _fallback_collection(temporary), False
    head = source_path.read_bytes()[:2]
    if head == b"PK":
        with zipfile.ZipFile(source_path) as archive:
            for member in archive.infolist():
                member_path = PurePosixPath(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError("APKG contains an unsafe path.")
            archive.extractall(temporary)
        collection = next(
            (
                temporary / name
                for name in _COLLECTION_NAMES
                if (temporary / name).is_file()
            ),
            None,
        )
        if collection is None:
            raise RuntimeError("APKG does not contain a supported Anki collection.")
    else:
        collection = temporary / "collection.anki2"
        shutil.copyfile(source_path, collection)
    if collection.read_bytes()[:4] == _ZSTD_MAGIC:
        database = temporary / "collection.sqlite"
        with collection.open("rb") as source_stream, database.open(
            "wb"
        ) as database_stream:
            zstandard.ZstdDecompressor().copy_stream(source_stream, database_stream)
        return database, True
    return collection, False
