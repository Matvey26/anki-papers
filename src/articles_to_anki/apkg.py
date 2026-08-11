from __future__ import annotations

import base64
import csv
import hashlib
import html
import json
import re
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

import zstandard


def plain_text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value))


def checksum(value: str) -> int:
    return int(hashlib.sha1(plain_text(value).encode("utf-8")).hexdigest()[:8], 16)


def guid(front: str) -> str:
    digest = hashlib.sha256(front.encode("utf-8")).digest()[:9]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def read_rows(paths: list[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not {"Front", "Back", "Tags"}.issubset(reader.fieldnames):
                raise ValueError("CSV must contain Front, Back and Tags columns.")
            rows.extend(reader)
    return rows


def write_combined_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Front", "Back", "Tags"], quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def merge(source: Path, destination: Path, csv_paths: list[Path], combined_csv: Path) -> None:
    rows = read_rows(csv_paths)
    write_combined_csv(combined_csv, rows)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="anki-merge-") as temporary_name:
        temporary = Path(temporary_name)
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                member_path = PurePosixPath(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError("APKG contains an unsafe path.")
            archive.extractall(temporary)

        collection_name = next(
            (
                name
                for name in ("collection.anki21b", "collection.anki21", "collection.anki2")
                if (temporary / name).is_file()
            ),
            None,
        )
        if collection_name is None:
            raise RuntimeError("APKG does not contain a supported Anki collection.")
        collection_path = temporary / collection_name
        is_compressed = collection_name.endswith("21b")
        if is_compressed:
            database = temporary / "collection.sqlite"
            with collection_path.open("rb") as source_stream, database.open(
                "wb"
            ) as database_stream:
                zstandard.ZstdDecompressor().copy_stream(
                    source_stream, database_stream
                )
        else:
            database = collection_path

        connection = sqlite3.connect(database)
        try:
            before = connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
            deck_id = _deck_id(connection)
            note_type_id = _note_type_id(connection)
            next_due = connection.execute(
                "SELECT COALESCE(MAX(due), 0) + 1 FROM cards WHERE queue = 0"
            ).fetchone()[0]
            now_seconds = int(time.time())
            max_card_id = connection.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM cards").fetchone()[0]
            max_note_id = connection.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM notes").fetchone()[0]
            base_id = max(int(time.time() * 1000), max_card_id, max_note_id)
            existing_identities = set()
            for fields, tags in connection.execute("SELECT flds, tags FROM notes"):
                front, _, back = fields.partition("\x1f")
                existing_identities.update(card_identities(front, back, tags))
            rows_to_add: list[dict[str, str]] = []
            for row in rows:
                identities = card_identities(row["Front"], row["Back"], row["Tags"])
                if identities & existing_identities:
                    continue
                existing_identities.update(identities)
                rows_to_add.append(row)

            for index, row in enumerate(rows_to_add):
                note_id = base_id + index
                front = row["Front"]
                back = row["Back"]
                tags = " " + " ".join(row["Tags"].split()) + " "
                fields = front + "\x1f" + back
                connection.execute(
                    "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        note_id,
                        guid(front),
                        note_type_id,
                        now_seconds,
                        -1,
                        tags,
                        fields,
                        plain_text(front),
                        checksum(front),
                        0,
                        "",
                    ),
                )
                connection.execute(
                    "INSERT INTO cards VALUES (?, ?, ?, 0, ?, -1, 0, 0, ?, 0, 0, 0, 0, 0, 0, 0, 0, '{}')",
                    (note_id, note_id, deck_id, now_seconds, next_due + index),
                )

            try:
                connection.execute(
                    "UPDATE config SET val = ?, usn = -1, mtime_secs = ? WHERE key = 'nextPos'",
                    (str(next_due + len(rows_to_add)), now_seconds),
                )
            except sqlite3.OperationalError:
                pass
            connection.execute("UPDATE col SET mod = ?", (int(time.time() * 1000),))
            connection.commit()
            after = connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
            if after != before + len(rows_to_add):
                raise RuntimeError(f"Card count mismatch: {before} + {len(rows_to_add)} != {after}")
        finally:
            connection.close()

        if is_compressed:
            with database.open("rb") as database_stream, collection_path.open(
                "wb"
            ) as collection_stream:
                zstandard.ZstdCompressor(level=10).copy_stream(
                    database_stream, collection_stream
                )
            database.unlink()
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
            for path in sorted(temporary.iterdir()):
                archive.write(path, path.name)


def _normalized_front(value: str) -> str:
    return " ".join(plain_text(value).casefold().split())


def managed_identities(source: Path) -> set[tuple[str, ...]]:
    """Read website card IDs and legacy context identities without changing APKG."""
    with tempfile.TemporaryDirectory(prefix="anki-identities-") as temporary_name:
        temporary = Path(temporary_name)
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                member_path = PurePosixPath(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError("APKG contains an unsafe path.")
            archive.extractall(temporary)
        collection_name = next(
            (
                name
                for name in ("collection.anki21b", "collection.anki21", "collection.anki2")
                if (temporary / name).is_file()
            ),
            None,
        )
        if collection_name is None:
            raise RuntimeError("APKG does not contain a supported Anki collection.")
        collection_path = temporary / collection_name
        if collection_name.endswith("21b"):
            database = temporary / "collection.sqlite"
            with collection_path.open("rb") as source_stream, database.open("wb") as database_stream:
                zstandard.ZstdDecompressor().copy_stream(source_stream, database_stream)
        else:
            database = collection_path
        connection = sqlite3.connect(database)
        try:
            identities: set[tuple[str, ...]] = set()
            for fields, tags in connection.execute("SELECT flds, tags FROM notes"):
                front, _, back = fields.partition("\x1f")
                identities.update(card_identities(front, back, tags))
            return identities
        finally:
            connection.close()


def card_identities(front: str, back: str, tags: str) -> set[tuple[str, ...]]:
    tag_set = set(tags.split())
    kind = "recall" if "card::recall" in tag_set else "meaning" if "card::meaning" in tag_set else ""
    if not kind or not any(tag.startswith("article::") for tag in tag_set):
        return {("front", _normalized_front(front))}
    identities: set[tuple[str, ...]] = set()
    site_tag = next((tag for tag in tag_set if tag.startswith("anki_papers::")), "")
    if site_tag:
        identities.add(("site", kind, site_tag.removeprefix("anki_papers::")))
    if kind == "meaning":
        match = re.search(r"<b\b[^>]*>(.*?)</b>", front, flags=re.IGNORECASE | re.DOTALL)
        if match:
            target = _normalized_front(match.group(1))
            if target:
                identities.add(("context", kind, target, _normalized_front(front)))
    else:
        target = _normalized_front(back)
        context = re.sub(r"<br\s*/?><small>.*?</small>\s*$", "", front, flags=re.IGNORECASE | re.DOTALL)
        match = re.search(r"<b\b[^>]*>.*?</b>", context, flags=re.IGNORECASE | re.DOTALL)
        if target and match:
            surrounding = (
                plain_text(context[: match.start()])
                + " __target__ "
                + plain_text(context[match.end() :])
            )
            identities.add(("context", kind, target, " ".join(surrounding.casefold().split())))
    return identities or {("front", _normalized_front(front))}


def _card_identity(front: str, back: str, tags: str) -> tuple[str, ...]:
    return sorted(card_identities(front, back, tags))[0]


def _deck_id(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT id FROM decks ORDER BY id LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        return int(row[0])
    decks_json = connection.execute("SELECT decks FROM col LIMIT 1").fetchone()[0]
    decks = json.loads(decks_json)
    if not decks:
        raise RuntimeError("APKG contains no decks.")
    return int(next(iter(decks)))


def _note_type_id(connection: sqlite3.Connection) -> int:
    try:
        models_json = connection.execute("SELECT models FROM col LIMIT 1").fetchone()[0]
        models = json.loads(models_json)
        for model_id, model in models.items():
            if len(model.get("flds", [])) == 2:
                return int(model_id)
    except (sqlite3.OperationalError, TypeError, ValueError, json.JSONDecodeError):
        pass
    row = connection.execute("SELECT mid FROM notes GROUP BY mid ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("APKG contains no compatible note type.")
    return int(row[0])
