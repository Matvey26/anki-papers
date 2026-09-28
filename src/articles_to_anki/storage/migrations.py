"""storage / migrations."""
from __future__ import annotations

import json
import sqlite3
import uuid

from articles_to_anki.cards.text import normalize_target


def card_context_key(row: sqlite3.Row) -> tuple[int, str, int, str, str]:
    return (
        int(row["user_id"]),
        str(row["document_id"]),
        int(row["page"]),
        normalize_target(row["target"]),
        " ".join(str(row["sentence"]).casefold().split()),
    )


def purge_llm_generated_contexts(connection: sqlite3.Connection) -> None:
    """Drop synthetic LLM contexts from legacy cards on startup."""
    rows = connection.execute(
        "SELECT id, contexts_json FROM cards WHERE semantic_version = 1"
    ).fetchall()
    for row in rows:
        contexts = json.loads(row["contexts_json"] or "[]")
        retained = [
            item
            for item in contexts
            if not (
                isinstance(item, dict)
                and item.get("source") == "llm_generated"
            )
        ]
        if len(retained) == len(contexts):
            continue
        connection.execute(
            "UPDATE cards SET contexts_json = ? WHERE id = ?",
            (json.dumps(retained, ensure_ascii=False), row["id"]),
        )


def migrate_cards_to_contexts(connection: sqlite3.Connection) -> None:
    schema_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cards'"
    ).fetchone()
    schema = "" if schema_row is None else "".join(str(schema_row[0]).split())
    if "UNIQUE(user_id,target_normalized)" not in schema:
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript(
        """
        DROP TABLE IF EXISTS anki_note_links;
        ALTER TABLE card_highlights RENAME TO card_highlights_by_target;
        ALTER TABLE cards RENAME TO cards_by_target;
        CREATE TABLE cards (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            target TEXT NOT NULL,
            target_normalized TEXT NOT NULL,
            sentence TEXT NOT NULL,
            page INTEGER NOT NULL,
            translations_json TEXT NOT NULL,
            replacement TEXT NOT NULL,
            alternatives_json TEXT NOT NULL,
            csv_exported_at TEXT,
            apkg_exported_at TEXT,
            anki_synced_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, document_id, page, target_normalized, sentence)
        );
        INSERT INTO cards
            (id, user_id, document_id, target, target_normalized, sentence, page,
             translations_json, replacement, alternatives_json, csv_exported_at,
             apkg_exported_at, anki_synced_at, created_at)
        SELECT id, user_id, document_id, target, target_normalized, sentence, page,
               translations_json, replacement, alternatives_json, csv_exported_at,
               apkg_exported_at, anki_synced_at, created_at
        FROM cards_by_target;
        DROP TABLE card_highlights_by_target;
        DROP TABLE cards_by_target;
        CREATE INDEX idx_cards_user_created ON cards(user_id, created_at);
        CREATE TABLE card_highlights (
            card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            highlight_id TEXT NOT NULL REFERENCES highlights(id) ON DELETE CASCADE,
            PRIMARY KEY(card_id, highlight_id)
        );
        CREATE UNIQUE INDEX idx_card_highlights_highlight
            ON card_highlights(highlight_id);
        CREATE TABLE anki_note_links (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            site_card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            direction TEXT NOT NULL CHECK(direction IN ('meaning', 'recall')),
            note_id INTEGER NOT NULL,
            note_guid TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id, site_card_id, direction),
            UNIQUE(user_id, note_id)
        );
        """
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")


def synchronize_card_highlights_by_context(connection: sqlite3.Connection) -> None:
    cards = connection.execute("SELECT * FROM cards").fetchall()
    semantic_cards_by_highlight = {
        str(row["highlight_id"]): row
        for row in connection.execute(
            """SELECT card_highlights.highlight_id, cards.*
               FROM card_highlights
               JOIN cards ON cards.id = card_highlights.card_id
               WHERE cards.semantic_version = 1"""
        ).fetchall()
    }
    cards_by_context = {card_context_key(row): row for row in cards}
    cards_by_target = {
        (int(row["user_id"]), normalize_target(row["target"])): row
        for row in cards
    }
    connection.execute("DELETE FROM card_highlights")
    highlights = connection.execute(
        "SELECT * FROM highlights WHERE status = 'ready' ORDER BY created_at"
    ).fetchall()
    for highlight in highlights:
        key = card_context_key(highlight)
        card = semantic_cards_by_highlight.get(str(highlight["id"]))
        if card is None:
            card = cards_by_context.get(key)
        if card is None:
            template = cards_by_target.get((key[0], key[3]))
            card_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO cards
                   (id, user_id, document_id, target, target_normalized, sentence,
                    page, translations_json, replacement, alternatives_json,
                    csv_exported_at, apkg_exported_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
                (
                    card_id,
                    highlight["user_id"],
                    highlight["document_id"],
                    highlight["target"],
                    key[3],
                    highlight["sentence"],
                    highlight["page"],
                    highlight["translations_json"] if template is None else template["translations_json"],
                    highlight["replacement"] if template is None else template["replacement"],
                    highlight["alternatives_json"] if template is None else template["alternatives_json"],
                    highlight["created_at"],
                ),
            )
            card = connection.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
            if card is None:
                raise RuntimeError("Failed to split card contexts")
            cards_by_context[key] = card
            cards_by_target[(key[0], key[3])] = card
        connection.execute(
            "INSERT INTO card_highlights (card_id, highlight_id) VALUES (?, ?)",
            (card["id"], highlight["id"]),
        )
