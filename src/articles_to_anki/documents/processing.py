"""documents / processing."""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask

from articles_to_anki.cards.article_contexts import refresh_article_contexts
from articles_to_anki.cards.clusters import MissingApiKeyError
from articles_to_anki.cards.enrichment import enrich_highlight_row
from articles_to_anki.cards.text import normalize_target, now
from articles_to_anki.documents.highlights import (
    clean_highlight_rects,
    delete_highlight_rows,
    highlight_rects_match,
)
from articles_to_anki.documents.pdf import write_pdf_without_native_highlights
from articles_to_anki.documents.text_cache import load_or_extract_document_text
from articles_to_anki.extract import extract_highlights
from articles_to_anki.storage.files import remove_managed_files
from articles_to_anki.sync_queue import enqueue_sync_job


def enqueue_document_processing(app: Flask, document_id: str, user_id: int) -> None:
    jobs: set[str] = app.extensions["highlight_jobs"]
    lock: threading.Lock = app.extensions["highlight_jobs_lock"]
    with lock:
        if document_id in jobs:
            return
        jobs.add(document_id)

    def run() -> None:
        try:
            process_document_highlights(app, document_id, user_id)
        finally:
            with lock:
                jobs.discard(document_id)
                deleted_paths = app.extensions["deleted_document_paths"].pop(
                    document_id,
                    None,
                )
            if deleted_paths:
                remove_managed_files(Path(app.config["DATA_DIR"]), deleted_paths)

    if app.config["PROCESS_DOCUMENTS_INLINE"]:
        run()
    else:
        executor: ThreadPoolExecutor = app.extensions["highlight_executor"]
        executor.submit(run)


def process_document_highlights(app: Flask, document_id: str, user_id: int) -> None:
    database = sqlite3.connect(app.config["DATABASE"], timeout=30)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    try:
        document = database.execute(
            """SELECT * FROM documents
               WHERE id = ? AND user_id = ? AND kind = 'pdf'""",
            (document_id, user_id),
        ).fetchone()
        if document is None:
            return
        database.execute(
            """UPDATE documents
               SET highlight_status = 'processing', highlight_error = NULL
               WHERE id = ? AND user_id = ?""",
            (document_id, user_id),
        )
        database.commit()

        stored_path = Path(document["stored_path"])
        if document["source_path"]:
            source_path = Path(document["source_path"])
        else:
            source_path = stored_path.with_name(f"{document_id}.source.pdf")
            shutil.copy2(stored_path, source_path)
            database.execute(
                "UPDATE documents SET source_path = ? WHERE id = ? AND user_id = ?",
                (str(source_path), document_id, user_id),
            )
            database.commit()

        document_text = load_or_extract_document_text(database, document)
        database.commit()
        extracted = extract_highlights(source_path, document_text=document_text)
        write_pdf_without_native_highlights(source_path, stored_path)
        database.execute(
            "DELETE FROM highlights WHERE document_id = ? AND user_id = ? AND source = 'pdf_import'",
            (document_id, user_id),
        )
        timestamp = now()
        imported_ids: list[str] = []
        for item in extracted:
            rects = clean_highlight_rects(item.rects)
            rects_json = json.dumps(rects, separators=(",", ":"))
            reader_candidates = database.execute(
                """SELECT target, rects_json FROM highlights
                   WHERE user_id = ? AND document_id = ? AND page = ?
                     AND source = 'reader'""",
                (user_id, document_id, item.context.source_page),
            ).fetchall()
            duplicate_reader_highlight = any(
                normalize_target(candidate["target"])
                == normalize_target(item.context.target)
                and highlight_rects_match(
                    rects,
                    clean_highlight_rects(json.loads(candidate["rects_json"])),
                )
                for candidate in reader_candidates
            )
            if duplicate_reader_highlight:
                continue
            identity = f"{item.context.source_page}:{rects_json}:{normalize_target(item.context.target)}"
            try:
                namespace = uuid.UUID(document_id)
            except ValueError:
                namespace = uuid.NAMESPACE_URL
                identity = f"{document_id}:{identity}"
            highlight_id = str(uuid.uuid5(namespace, identity))
            deleted = database.execute(
                """SELECT 1 FROM deleted_highlights
                   WHERE highlight_id = ? AND user_id = ? AND document_id = ?""",
                (highlight_id, user_id, document_id),
            ).fetchone()
            if deleted is not None:
                continue
            database.execute(
                """INSERT OR IGNORE INTO highlights
                   (id, user_id, document_id, target, sentence, page, rects_json,
                    translations_json, replacement, alternatives_json, status,
                    error, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '', '[]', 'pending',
                           NULL, 'pdf_import', ?, ?)""",
                (
                    highlight_id,
                    user_id,
                    document_id,
                    item.context.target,
                    item.context.sentence,
                    item.context.source_page,
                    rects_json,
                    timestamp,
                    timestamp,
                ),
            )
            row = database.execute(
                "SELECT id, source FROM highlights WHERE user_id = ? AND document_id = ? AND page = ? AND rects_json = ?",
                (user_id, document_id, item.context.source_page, rects_json),
            ).fetchone()
            if row is not None and row["source"] == "pdf_import":
                imported_ids.append(row["id"])
        database.commit()

        discarded = 0
        for highlight_id in imported_ids:
            row = database.execute(
                "SELECT * FROM highlights WHERE id = ? AND user_id = ?",
                (highlight_id, user_id),
            ).fetchone()
            if row is None:
                continue
            try:
                enrich_highlight_row(
                    app,
                    database,
                    row,
                    user_id=user_id,
                    document_id=document_id,
                    mine_article_contexts=False,
                )
            except MissingApiKeyError:
                discarded += 1
                app.logger.error(
                    "OPENROUTER_API_KEY is missing; discarding imported highlight"
                )
                delete_highlight_rows(database, [row], user_id=user_id)
            except Exception:
                discarded += 1
                app.logger.exception("Imported highlight enrichment failed")
                delete_highlight_rows(database, [row], user_id=user_id)

        article_context_cards = refresh_article_contexts(
            database, user_id, data_dir=Path(app.config["DATA_DIR"])
        )
        if article_context_cards:
            account = database.execute(
                "SELECT state FROM anki_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()
            if account is not None and account["state"] in {"connected", "syncing", "error"}:
                enqueue_sync_job(database, user_id, "article_contexts", delay_seconds=30)

        imported_count = database.execute(
            """SELECT COUNT(*) FROM highlights
               WHERE document_id = ? AND user_id = ? AND source = 'pdf_import'""",
            (document_id, user_id),
        ).fetchone()[0]
        database.execute(
            """UPDATE documents
               SET highlight_status = 'ready', highlight_error = NULL,
                   highlight_processed_at = ?, imported_highlight_count = ?
               WHERE id = ? AND user_id = ?""",
            (now(), imported_count, document_id, user_id),
        )
        if discarded:
            app.logger.warning(
                "Discarded %s highlights after enrichment retries", discarded
            )
        database.commit()
    except Exception:
        app.logger.exception("PDF highlight processing failed")
        database.rollback()
        database.execute(
            """UPDATE documents
               SET highlight_status = 'failed', highlight_error = ?
               WHERE id = ? AND user_id = ?""",
            ("Не удалось обработать хайлайты в PDF.", document_id, user_id),
        )
        database.commit()
    finally:
        database.close()
