"""storage / schema."""
from __future__ import annotations

import sqlite3

from flask import Flask

from articles_to_anki.cards.text import now
from articles_to_anki.storage.migrations import (
    migrate_cards_to_contexts,
    purge_llm_generated_contexts,
    synchronize_card_highlights_by_context,
)


def init_database(app: Flask) -> None:
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT,
                password_set_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('pdf', 'apkg')),
                name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                text_path TEXT,
               page_count INTEGER NOT NULL DEFAULT 0,
               size INTEGER NOT NULL,
               read_at TEXT,
                last_page INTEGER,
                last_opened_at TEXT,
               source_path TEXT,
                highlight_status TEXT NOT NULL DEFAULT 'idle'
                    CHECK(highlight_status IN ('idle', 'queued', 'processing', 'ready', 'failed')),
                highlight_error TEXT,
                highlight_processed_at TEXT,
                imported_highlight_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_documents_user_kind
                ON documents(user_id, kind, created_at);
            CREATE TABLE IF NOT EXISTS highlights (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                target TEXT NOT NULL,
                sentence TEXT NOT NULL,
                page INTEGER NOT NULL,
                rects_json TEXT NOT NULL,
                translations_json TEXT NOT NULL DEFAULT '[]',
                replacement TEXT NOT NULL DEFAULT '',
                alternatives_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'ready', 'failed')),
                error TEXT,
                source TEXT NOT NULL DEFAULT 'reader'
                    CHECK(source IN ('reader', 'pdf_import')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, document_id, page, rects_json)
            );
            CREATE INDEX IF NOT EXISTS idx_highlights_document
                ON highlights(user_id, document_id, page, created_at);
            CREATE TABLE IF NOT EXISTS cards (
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
                lemma TEXT,
                family_key TEXT,
                part_of_speech TEXT,
                sense_definition_en TEXT,
                contexts_json TEXT,
                semantic_version INTEGER NOT NULL DEFAULT 0,
                csv_exported_at TEXT,
                apkg_exported_at TEXT,
                anki_synced_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, document_id, page, target_normalized, sentence)
            );
            CREATE INDEX IF NOT EXISTS idx_cards_user_created
                ON cards(user_id, created_at);
            CREATE TABLE IF NOT EXISTS card_highlights (
                card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                highlight_id TEXT NOT NULL REFERENCES highlights(id) ON DELETE CASCADE,
                PRIMARY KEY(card_id, highlight_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_card_highlights_highlight
                ON card_highlights(highlight_id);
            CREATE TABLE IF NOT EXISTS deleted_highlights (
                highlight_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_deleted_highlights_document
                ON deleted_highlights(user_id, document_id);
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE,
                ip_address TEXT NOT NULL,
                successful INTEGER NOT NULL CHECK(successful IN (0, 1)),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup
                ON login_attempts(username, ip_address, created_at);
            CREATE TABLE IF NOT EXISTS account_claim_tokens (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_claim_tokens_user
                ON account_claim_tokens(user_id, expires_at);
            CREATE TABLE IF NOT EXISTS user_credentials (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                ankiweb_id_ciphertext BLOB NOT NULL,
                ankiweb_id_nonce BLOB NOT NULL,
                password_ciphertext BLOB NOT NULL,
                password_nonce BLOB NOT NULL,
                hkey_ciphertext BLOB,
                hkey_nonce BLOB,
                key_version INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK(state IN ('pending', 'active', 'needs_reconnect')),
                auth_failures INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS anki_accounts (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                selected_deck_id INTEGER,
                selected_deck_name TEXT,
                available_decks_json TEXT NOT NULL DEFAULT '[]',
                mirror_path TEXT,
                mirror_nonce BLOB,
                mirror_key_version INTEGER,
                state TEXT NOT NULL DEFAULT 'connecting'
                    CHECK(state IN ('connecting', 'awaiting_deck', 'connected', 'syncing', 'needs_reconnect', 'error')),
                last_success_at TEXT,
                last_error TEXT,
                last_added_count INTEGER NOT NULL DEFAULT 0,
                preview_existing INTEGER NOT NULL DEFAULT 0,
                preview_missing INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS anki_note_links (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                site_card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                direction TEXT NOT NULL CHECK(direction IN ('meaning', 'recall')),
                note_id INTEGER NOT NULL,
                note_guid TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, site_card_id, direction),
                UNIQUE(user_id, note_id)
            );
            CREATE TABLE IF NOT EXISTS sync_jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reason TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued'
                    CHECK(state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
                attempts INTEGER NOT NULL DEFAULT 0,
                run_after TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_jobs_one_queued_user
                ON sync_jobs(user_id) WHERE state = 'queued';
            CREATE INDEX IF NOT EXISTS idx_sync_jobs_ready
                ON sync_jobs(state, run_after, created_at);
            CREATE TABLE IF NOT EXISTS rebuild_jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                state TEXT NOT NULL DEFAULT 'queued'
                    CHECK(state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
                deck_id INTEGER,
                progress INTEGER NOT NULL DEFAULT 0,
                stage TEXT NOT NULL DEFAULT '',
                error TEXT,
                result_path TEXT,
                uploaded_to TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rebuild_jobs_one_active_user
                ON rebuild_jobs(user_id) WHERE state IN ('queued', 'running');
            CREATE INDEX IF NOT EXISTS idx_rebuild_jobs_user
                ON rebuild_jobs(user_id, created_at);
            CREATE TABLE IF NOT EXISTS worker_heartbeat (
                worker_name TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL
            );
            """
        )
        document_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(documents)")
        }
        rebuild_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rebuild_jobs)")
        }
        if "uploaded_to" not in rebuild_columns:
            connection.execute("ALTER TABLE rebuild_jobs ADD COLUMN uploaded_to TEXT")
        if "read_at" not in document_columns:
            connection.execute("ALTER TABLE documents ADD COLUMN read_at TEXT")
        document_migrations = {
            "text_path": "TEXT",
            "last_page": "INTEGER",
            "last_opened_at": "TEXT",
            "source_path": "TEXT",
            "highlight_status": "TEXT NOT NULL DEFAULT 'idle'",
            "highlight_error": "TEXT",
            "highlight_processed_at": "TEXT",
            "imported_highlight_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in document_migrations.items():
            if column not in document_columns:
                connection.execute(
                    f"ALTER TABLE documents ADD COLUMN {column} {declaration}"
                )
        highlight_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(highlights)")
        }
        if "source" not in highlight_columns:
            connection.execute(
                "ALTER TABLE highlights ADD COLUMN source TEXT NOT NULL DEFAULT 'reader'"
            )
        user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        if "password_hash" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        if "password_set_at" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN password_set_at TEXT")
        card_columns = {row[1] for row in connection.execute("PRAGMA table_info(cards)")}
        if "anki_synced_at" not in card_columns:
            connection.execute("ALTER TABLE cards ADD COLUMN anki_synced_at TEXT")
        migrate_cards_to_contexts(connection)
        card_columns = {row[1] for row in connection.execute("PRAGMA table_info(cards)")}
        semantic_columns = {
            "lemma": "TEXT",
            "family_key": "TEXT",
            "part_of_speech": "TEXT",
            "sense_definition_en": "TEXT",
            "contexts_json": "TEXT",
            "semantic_version": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in semantic_columns.items():
            if column not in card_columns:
                connection.execute(f"ALTER TABLE cards ADD COLUMN {column} {declaration}")
        connection.execute("DROP INDEX IF EXISTS idx_cards_semantic_family_lookup")
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_cards_cluster_leader
               ON cards(user_id, semantic_version, target_normalized)"""
        )
        account_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(anki_accounts)")
        }
        if "preview_existing" not in account_columns:
            connection.execute(
                "ALTER TABLE anki_accounts ADD COLUMN preview_existing INTEGER NOT NULL DEFAULT 0"
            )
        if "preview_missing" not in account_columns:
            connection.execute(
                "ALTER TABLE anki_accounts ADD COLUMN preview_missing INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """INSERT OR IGNORE INTO deleted_highlights
               (highlight_id, user_id, document_id, created_at)
               SELECT id, user_id, document_id, ? FROM highlights
               WHERE status = 'failed'""",
            (now(),),
        )
        connection.execute("DELETE FROM highlights WHERE status = 'failed'")
        purge_llm_generated_contexts(connection)
        synchronize_card_highlights_by_context(connection)
        connection.execute("PRAGMA optimize")
        connection.commit()
    finally:
        connection.close()
