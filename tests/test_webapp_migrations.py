from __future__ import annotations

import sqlite3
from pathlib import Path

from support_webapp import identify, make_app


def test_legacy_card_schema_migration_preserves_data_and_note_link_fk(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    identify(app.test_client(), "migration-user")
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
        database.execute(
            """INSERT INTO documents
               (id, user_id, kind, name, stored_path, page_count, size, created_at)
               VALUES ('doc', ?, 'pdf', 'paper.pdf', '/tmp/paper.pdf', 1, 1, '2026-01-01')""",
            (user_id,),
        )
        database.execute(
            """INSERT INTO cards
               (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, created_at)
               VALUES ('legacy-card', ?, 'doc', 'robust', 'robust', 'A robust test.', 1,
                       '["надёжный"]', 'надёжный', '["strong"]', '2026-01-01')""",
            (user_id,),
        )
        database.commit()
        database.execute("PRAGMA foreign_keys = OFF")
        database.executescript(
            """
            DROP TABLE card_highlights;
            DROP TABLE anki_note_links;
            ALTER TABLE cards RENAME TO cards_current;
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
                created_at TEXT NOT NULL,
                UNIQUE(user_id, target_normalized)
            );
            INSERT INTO cards
            SELECT id, user_id, document_id, target, target_normalized, sentence, page,
                   translations_json, replacement, alternatives_json, csv_exported_at,
                   apkg_exported_at, created_at FROM cards_current;
            DROP TABLE cards_current;
            CREATE TABLE card_highlights (
                card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                highlight_id TEXT NOT NULL REFERENCES highlights(id) ON DELETE CASCADE,
                PRIMARY KEY(card_id, highlight_id)
            );
            """
        )
        database.commit()

    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute(
            "SELECT target, sentence FROM cards WHERE id = 'legacy-card'"
        ).fetchone() == ("robust", "A robust test.")
        link_targets = {
            row[2] for row in database.execute("PRAGMA foreign_key_list(anki_note_links)")
        }
        assert "cards" in link_targets


def test_existing_database_gets_read_at_migration(tmp_path: Path) -> None:
    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute("ALTER TABLE documents DROP COLUMN read_at")
    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(documents)")}
    assert {"read_at", "last_page", "last_opened_at"} <= columns
