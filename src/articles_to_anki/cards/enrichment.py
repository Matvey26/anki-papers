"""cards / enrichment."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask

from articles_to_anki.cards.article_contexts import refresh_article_contexts
from articles_to_anki.cards.clusters import (
    MissingApiKeyError,
    find_cluster_candidates,
    merge_semantic_translations,
)
from articles_to_anki.cards.llm_cache import _llm_cache_dir, cluster_analysis_cached
from articles_to_anki.cards.text import normalize_selected_text, normalize_target, now
from articles_to_anki.documents.highlights import delete_highlight_rows
from articles_to_anki.enrichment.prompts import DEFAULT_SEMANTIC_MODEL
from articles_to_anki.sync_queue import enqueue_sync_job


def enrich_highlight_row(
    app: Flask,
    database: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    user_id: int,
    document_id: str,
    mine_article_contexts: bool = True,
) -> sqlite3.Row:
    timestamp = now()
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise MissingApiKeyError
    model = os.environ.get("OPENROUTER_SEMANTIC_MODEL", DEFAULT_SEMANTIC_MODEL)
    normalized_highlight = normalize_selected_text(str(row["target"]))
    candidates = find_cluster_candidates(database, user_id, normalized_highlight)
    analysis, _cached = cluster_analysis_cached(
        _llm_cache_dir(Path(app.config["DATA_DIR"])),
        user_id,
        model=model,
        target=str(row["target"]),
        normalized_target=normalized_highlight,
        sentence=str(row["sentence"]),
        candidates=candidates,
        api_key=api_key,
    )
    candidates_by_id = {item.cluster_id: item for item in candidates}
    selected_candidate = candidates_by_id.get(analysis.cluster_id)
    leader = (
        selected_candidate.leader
        if selected_candidate is not None
        else normalize_selected_text(analysis.leader) or normalized_highlight
    )
    translations = json.dumps(analysis.translations_ru, ensure_ascii=False)
    replacement = analysis.replacement_ru
    alternatives = "[]"
    source_context = {
        "id": str(row["id"]), "source": "user_pdf", "target": str(row["target"]),
        "sentence": str(row["sentence"]), "replacement": replacement,
        "translations": analysis.translations_ru, "lemma": leader,
        "family_key": leader, "part_of_speech": analysis.part_of_speech,
        "sense_definition_en": analysis.cluster_definition_en,
        "substitutes_en": analysis.source_distractors.substitutes_en,
        "related_en": analysis.source_distractors.related_en,
        "valid_substitutes_en": analysis.source_distractors.valid_substitutes_en,
        "valid_related_en": analysis.source_distractors.valid_related_en,
    }
    if selected_candidate is not None:
        existing = database.execute(
            "SELECT * FROM cards WHERE id = ? AND user_id = ?",
            (selected_candidate.cluster_id, user_id),
        ).fetchone()
        if existing is None:
            raise RuntimeError("Selected semantic card disappeared")
        contexts = json.loads(existing["contexts_json"] or "[]")
        known = {str(item.get("id")) for item in contexts if isinstance(item, dict)}
        known_sentences = {
            " ".join(str(item.get("sentence") or "").casefold().split())
            for item in contexts
            if isinstance(item, dict) and item.get("sentence")
        }
        source_sentence = " ".join(str(row["sentence"]).casefold().split())
        added_contexts = (
            []
            if source_sentence in known_sentences
            else [source_context]
        )
        contexts.extend(item for item in added_contexts if item["id"] not in known)
        card_translations = merge_semantic_translations(
            json.loads(existing["translations_json"]),
            analysis.translations_ru,
        )
        database.execute(
            """UPDATE cards SET contexts_json = ?, translations_json = ?,
               sense_definition_en = ?, csv_exported_at = NULL,
               apkg_exported_at = NULL, anki_synced_at = NULL WHERE id = ? AND user_id = ?""",
            (
                json.dumps(contexts, ensure_ascii=False),
                json.dumps(card_translations, ensure_ascii=False),
                analysis.cluster_definition_en,
                existing["id"],
                user_id,
            ),
        )
    else:
        card_id = str(uuid.uuid4())
        database.execute(
            """INSERT INTO cards
                (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, lemma, family_key,
                part_of_speech, sense_definition_en, contexts_json, semantic_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                card_id, user_id, document_id, leader,
                normalize_target(leader),
                row["sentence"], row["page"], translations, replacement, alternatives,
                leader, leader,
                analysis.part_of_speech.casefold(),
                analysis.cluster_definition_en, json.dumps([source_context], ensure_ascii=False), timestamp,
            ),
        )
        existing = database.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if existing is None:
        raise RuntimeError("Semantic card disappeared during enrichment")
    database.execute(
        """INSERT OR IGNORE INTO card_highlights (card_id, highlight_id)
           VALUES (?, ?)""",
        (existing["id"], row["id"]),
    )

    database.execute(
        """UPDATE highlights
           SET translations_json = ?, replacement = ?, alternatives_json = ?,
               status = 'ready', error = NULL, updated_at = ?
           WHERE id = ? AND user_id = ?""",
        (translations, replacement, alternatives, timestamp, row["id"], user_id),
    )
    account = database.execute(
        "SELECT state FROM anki_accounts WHERE user_id = ?", (user_id,)
    ).fetchone()
    if account is not None and account["state"] in {"connected", "syncing", "error"}:
        enqueue_sync_job(database, user_id, "card_saved", delay_seconds=30)
    database.commit()
    ready = database.execute(
        "SELECT * FROM highlights WHERE id = ? AND user_id = ?",
        (row["id"], user_id),
    ).fetchone()
    if ready is None:
        raise RuntimeError("Highlight disappeared during enrichment")
    if mine_article_contexts:
        try:
            changed = refresh_article_contexts(
                database,
                user_id,
                card_ids={str(existing["id"])},
                data_dir=Path(app.config["DATA_DIR"]),
            )
            if changed and account is not None and account["state"] in {
                "connected", "syncing", "error"
            }:
                enqueue_sync_job(
                    database, user_id, "article_contexts", delay_seconds=30
                )
            database.commit()
        except Exception:
            database.rollback()
            app.logger.exception(
                "Article context mining failed after highlight was saved"
            )
    return ready


def enqueue_reader_highlight_enrichment(
    app: Flask,
    *,
    highlight_id: str,
    user_id: int,
    document_id: str,
) -> None:
    jobs: set[str] = app.extensions["reader_highlight_jobs"]
    lock: threading.Lock = app.extensions["highlight_jobs_lock"]
    with lock:
        if highlight_id in jobs:
            return
        jobs.add(highlight_id)

    def run() -> None:
        database = sqlite3.connect(app.config["DATABASE"], timeout=30)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        try:
            row = database.execute(
                """SELECT * FROM highlights
                   WHERE id = ? AND user_id = ? AND document_id = ?""",
                (highlight_id, user_id, document_id),
            ).fetchone()
            if row is None or row["status"] == "ready":
                return
            enrich_highlight_row(
                app,
                database,
                row,
                user_id=user_id,
                document_id=document_id,
            )
        except MissingApiKeyError:
            app.logger.error("OPENROUTER_API_KEY is missing; discarding highlight")
            if "row" in locals() and row is not None:
                delete_highlight_rows(database, [row], user_id=user_id)
        except Exception:
            app.logger.exception("Automatic highlight enrichment failed")
            if "row" in locals() and row is not None:
                delete_highlight_rows(database, [row], user_id=user_id)
        finally:
            database.close()
            with lock:
                jobs.discard(highlight_id)

    if app.config["PROCESS_DOCUMENTS_INLINE"]:
        run()
    else:
        executor: ThreadPoolExecutor = app.extensions["highlight_executor"]
        executor.submit(run)
