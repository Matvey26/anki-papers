from __future__ import annotations

import argparse
import csv
import hashlib
import html
import re
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path


def plain_text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value))


def checksum(value: str) -> int:
    return int(hashlib.sha1(plain_text(value).encode("utf-8")).hexdigest()[:8], 16)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def row_key(row: dict[str, str]) -> tuple[str, str]:
    if "card::meaning" in row["Tags"]:
        return "meaning", row["Front"]
    return "recall", " ".join(row["Tags"].split()) + "\0" + row["Back"]


def refresh(source: Path, destination: Path, new_csv: Path) -> None:
    new_rows = read_csv(new_csv)
    new_by_key = {row_key(row): row for row in new_rows}
    if len(new_by_key) != len(new_rows):
        raise RuntimeError("New CSV contains ambiguous card identities")

    with tempfile.TemporaryDirectory(prefix="anki-refresh-") as temporary_name:
        temporary = Path(temporary_name)
        with zipfile.ZipFile(source) as archive:
            archive.extractall(temporary)
        compressed = temporary / "collection.anki21b"
        database = temporary / "collection21.sqlite"
        subprocess.run(["zstd", "-q", "-d", "-f", str(compressed), "-o", str(database)], check=True)

        connection = sqlite3.connect(database)
        try:
            notes = connection.execute(
                "SELECT id, tags, flds FROM notes WHERE tags LIKE '%article::v2%'"
            ).fetchall()
            now_seconds = int(time.time())
            updated = 0
            for note_id, tags, fields in notes:
                front, back = fields.split("\x1f", 1)
                kind = "meaning" if "card::meaning" in tags else "recall"
                key = (kind, front) if kind == "meaning" else (
                    kind,
                    " ".join(tags.split()) + "\0" + back,
                )
                new_row = new_by_key.get(key)
                if new_row is None:
                    raise RuntimeError(f"No Luna replacement for v2 note {note_id}: {front[:100]}")
                new_fields = new_row["Front"] + "\x1f" + new_row["Back"]
                connection.execute(
                    "UPDATE notes SET flds=?, sfld=?, csum=?, mod=?, usn=-1 WHERE id=?",
                    (new_fields, plain_text(new_row["Front"]), checksum(new_row["Front"]), now_seconds, note_id),
                )
                updated += 1

            dummy = connection.execute(
                "SELECT id FROM notes WHERE flds = ?", ("Front\x1fBack",)
            ).fetchall()
            for (note_id,) in dummy:
                card_ids = [row[0] for row in connection.execute("SELECT id FROM cards WHERE nid=?", (note_id,))]
                for card_id in card_ids:
                    connection.execute("DELETE FROM revlog WHERE cid=?", (card_id,))
                connection.execute("DELETE FROM cards WHERE nid=?", (note_id,))
                connection.execute("DELETE FROM notes WHERE id=?", (note_id,))

            if updated != len(new_rows):
                raise RuntimeError(f"Updated {updated} of {len(new_rows)} v2 notes")
            connection.execute("UPDATE col SET mod=?", (int(time.time() * 1000),))
            connection.commit()
        finally:
            connection.close()

        subprocess.run(["zstd", "-q", "-f", "-19", str(database), "-o", str(compressed)], check=True)
        database.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
            for path in sorted(temporary.iterdir()):
                archive.write(path, path.name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("new_csv", type=Path)
    args = parser.parse_args()
    refresh(args.source, args.destination, args.new_csv)


if __name__ == "__main__":
    main()
