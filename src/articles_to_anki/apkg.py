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
from collections import defaultdict
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
            removed_duplicates = _deduplicate_managed_cards(connection)
            deck_id = _deck_id(connection)
            note_type_id = _note_type_id(connection)
            next_due = connection.execute(
                "SELECT COALESCE(MAX(due), 0) + 1 FROM cards WHERE queue = 0"
            ).fetchone()[0]
            now_seconds = int(time.time())
            max_card_id = connection.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM cards").fetchone()[0]
            max_note_id = connection.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM notes").fetchone()[0]
            base_id = max(int(time.time() * 1000), max_card_id, max_note_id)
            existing_identities = {
                _card_identity(
                    fields.split("\x1f", 1)[0],
                    fields.split("\x1f", 1)[1] if "\x1f" in fields else "",
                    tags,
                )
                for fields, tags in connection.execute("SELECT flds, tags FROM notes")
            }
            rows_to_add: list[dict[str, str]] = []
            for row in rows:
                identity = _card_identity(row["Front"], row["Back"], row["Tags"])
                if identity in existing_identities:
                    continue
                existing_identities.add(identity)
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
            expected = before - removed_duplicates + len(rows_to_add)
            if after != expected:
                raise RuntimeError(f"Card count mismatch: expected {expected}, got {after}")
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


def managed_identities(source: Path) -> set[tuple[str, str]]:
    """Return website-card identities already present in an APKG."""
    with tempfile.TemporaryDirectory(prefix="anki-identities-") as temporary_name:
        collection_path, database, _ = _extract_collection(
            source, Path(temporary_name)
        )
        connection = sqlite3.connect(database)
        try:
            return {
                identity
                for fields, tags in connection.execute("SELECT flds, tags FROM notes")
                if (identity := _managed_identity_from_fields(fields, tags)) is not None
            }
        finally:
            connection.close()


def _extract_collection(source: Path, temporary: Path) -> tuple[Path, Path, bool]:
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
    if not is_compressed:
        return collection_path, collection_path, False
    database = temporary / "collection.sqlite"
    with collection_path.open("rb") as source_stream, database.open("wb") as database_stream:
        zstandard.ZstdDecompressor().copy_stream(source_stream, database_stream)
    return collection_path, database, True


def _managed_identity_from_fields(fields: str, tags: str) -> tuple[str, str] | None:
    front, separator, back = fields.partition("\x1f")
    if not separator:
        back = ""
    tag_set = set(tags.split())
    if not any(tag.startswith("article::") for tag in tag_set):
        return None
    if "card::recall" in tag_set:
        target = _normalized_front(back)
        return ("recall", target) if target else None
    if "card::meaning" in tag_set:
        match = re.search(r"<b\b[^>]*>(.*?)</b>", front, flags=re.IGNORECASE | re.DOTALL)
        if match and (target := _normalized_front(match.group(1))):
            return "meaning", target
    return None


def _deduplicate_managed_cards(connection: sqlite3.Connection) -> int:
    """Remove duplicate website cards, preserving the most useful schedule."""
    connection.row_factory = sqlite3.Row
    has_revlog = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'revlog'"
    ).fetchone()
    review_columns = (
        "(SELECT COUNT(*) FROM revlog WHERE revlog.cid = cards.id) AS review_count, "
        "(SELECT COALESCE(MAX(id), 0) FROM revlog WHERE revlog.cid = cards.id) AS last_review"
        if has_revlog
        else "0 AS review_count, 0 AS last_review"
    )
    rows = connection.execute(
        f"""SELECT notes.id AS nid, notes.flds, notes.tags,
                   cards.id AS cid, cards.type, cards.queue, cards.due,
                   cards.ivl, cards.reps, cards.mod, {review_columns}
            FROM notes JOIN cards ON cards.nid = notes.id"""
    ).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        identity = _managed_identity_from_fields(row["flds"], row["tags"])
        if identity is not None:
            groups[identity].append(row)

    losing_cards: list[tuple[int, int]] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        scheduled = [row for row in group if _has_schedule(row)]
        candidates = scheduled or group
        winner = max(
            candidates,
            key=lambda row: (
                int(row["due"]) if scheduled else 0,
                int(row["last_review"]),
                int(row["mod"]),
                int(row["cid"]),
            ),
        )
        losing_cards.extend(
            (int(row["cid"]), int(row["nid"]))
            for row in group
            if row["cid"] != winner["cid"]
        )

    for card_id, note_id in losing_cards:
        if has_revlog:
            connection.execute("DELETE FROM revlog WHERE cid = ?", (card_id,))
        connection.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        if not connection.execute(
            "SELECT 1 FROM cards WHERE nid = ? LIMIT 1", (note_id,)
        ).fetchone():
            connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    return len(losing_cards)


def _has_schedule(row: sqlite3.Row) -> bool:
    return bool(
        row["review_count"]
        or row["reps"]
        or row["type"]
        or row["queue"] not in (0, -1)
    )


def _normalized_front(value: str) -> str:
    return " ".join(plain_text(value).casefold().split())


def _card_identity(front: str, back: str, tags: str) -> tuple[str, str]:
    if identity := _managed_identity_from_fields(front + "\x1f" + back, tags):
        return identity
    return "front", _normalized_front(front)


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
