"""web / exports."""
from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

from flask import session

from articles_to_anki.cards.rendering import cards_to_csv
from articles_to_anki.cards.text import now
from articles_to_anki.storage.database import get_database


def pending_cards(column: str) -> list[sqlite3.Row]:
    if column not in {"csv_exported_at", "apkg_exported_at"}:
        raise ValueError("Unknown export column")
    return get_database().execute(
        f"""SELECT cards.*, documents.name AS document_name
            FROM cards JOIN documents ON documents.id = cards.document_id
            WHERE cards.user_id = ? AND cards.{column} IS NULL
            ORDER BY cards.created_at""",
        (session["user_id"],),
    ).fetchall()


def mark_exported(cards: list[sqlite3.Row], column: str) -> None:
    if column not in {"csv_exported_at", "apkg_exported_at"}:
        raise ValueError("Unknown export column")
    timestamp = now()
    get_database().executemany(
        f"UPDATE cards SET {column} = ? WHERE id = ? AND user_id = ?",
        [(timestamp, card["id"], session["user_id"]) for card in cards],
    )
    get_database().commit()


def reconcile_apkg_exports(deck: sqlite3.Row) -> None:
    from articles_to_anki.apkg import card_identities, managed_identities

    deck_identities = managed_identities(Path(deck["stored_path"]))
    cards = get_database().execute(
        """SELECT cards.*, documents.name AS document_name
           FROM cards JOIN documents ON documents.id = cards.document_id
           WHERE cards.user_id = ? ORDER BY cards.created_at""",
        (session["user_id"],),
    ).fetchall()
    timestamp = now()
    updates = []
    for card in cards:
        rows = csv.DictReader(
            io.StringIO(cards_to_csv([card]).decode("utf-8-sig"))
        )
        complete = all(
            bool(card_identities(row["Front"], row["Back"], row["Tags"]) & deck_identities)
            for row in rows
        )
        exported_at = (card["apkg_exported_at"] or timestamp) if complete else None
        updates.append((exported_at, card["id"], session["user_id"]))
    get_database().executemany(
        "UPDATE cards SET apkg_exported_at = ? WHERE id = ? AND user_id = ?",
        updates,
    )
    get_database().commit()
