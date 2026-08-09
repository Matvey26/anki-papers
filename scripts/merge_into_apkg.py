from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import re
import shutil
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


def guid(index: int, front: str) -> str:
    digest = hashlib.sha256(f"{index}\0{front}".encode("utf-8")).digest()[:9]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def read_rows(paths: list[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in paths:
        with path.open(encoding="utf-8", newline="") as stream:
            rows.extend(csv.DictReader(stream))
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
            archive.extractall(temporary)
        compressed = temporary / "collection.anki21b"
        database = temporary / "collection21.sqlite"
        subprocess.run(["zstd", "-q", "-d", "-f", str(compressed), "-o", str(database)], check=True)

        connection = sqlite3.connect(database)
        try:
            before = connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
            deck_id = connection.execute("SELECT id FROM decks ORDER BY id LIMIT 1").fetchone()[0]
            note_type_id = connection.execute(
                "SELECT mid FROM notes GROUP BY mid ORDER BY COUNT(*) DESC LIMIT 1"
            ).fetchone()[0]
            next_due = connection.execute(
                "SELECT COALESCE(MAX(due), 0) + 1 FROM cards WHERE queue = 0"
            ).fetchone()[0]
            now_seconds = int(time.time())
            base_id = max(int(time.time() * 1000), connection.execute("SELECT MAX(id) + 1 FROM cards").fetchone()[0])

            for index, row in enumerate(rows):
                note_id = base_id + index
                front = row["Front"]
                back = row["Back"]
                tags = " " + " ".join(row["Tags"].split()) + " "
                fields = front + "\x1f" + back
                sort_field = plain_text(front)
                connection.execute(
                    "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (note_id, guid(index, front), note_type_id, now_seconds, -1, tags, fields, sort_field, checksum(front), 0, ""),
                )
                connection.execute(
                    "INSERT INTO cards VALUES (?, ?, ?, 0, ?, -1, 0, 0, ?, 0, 0, 0, 0, 0, 0, 0, 0, '{}')",
                    (note_id, note_id, deck_id, now_seconds, next_due + index),
                )

            connection.execute(
                "UPDATE config SET val = ?, usn = -1, mtime_secs = ? WHERE key = 'nextPos'",
                (str(next_due + len(rows)), now_seconds),
            )
            connection.execute("UPDATE col SET mod = ?", (int(time.time() * 1000),))
            connection.commit()
            after = connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
            if after != before + len(rows):
                raise RuntimeError(f"Card count mismatch: {before} + {len(rows)} != {after}")
        finally:
            connection.close()

        subprocess.run(["zstd", "-q", "-f", "-19", str(database), "-o", str(compressed)], check=True)
        database.unlink()
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
            for path in sorted(temporary.iterdir()):
                archive.write(path, path.name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("combined_csv", type=Path)
    parser.add_argument("csv", type=Path, nargs="+")
    args = parser.parse_args()
    merge(args.source, args.destination, args.csv, args.combined_csv)


if __name__ == "__main__":
    main()
