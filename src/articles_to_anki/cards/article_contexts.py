"""cards / article_contexts."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from pypdf.errors import PdfReadError

from articles_to_anki.cards.context_selection import (
    _article_context_candidate,
    _article_context_is_readable,
    _article_context_score,
    _select_article_context_candidates,
)
from articles_to_anki.cards.llm_cache import _llm_cache_dir, context_approval_cached
from articles_to_anki.constants import (
    CONTEXT_APPROVAL_CANDIDATE_LIMIT,
    CONTEXT_CANDIDATES_PER_DOCUMENT,
    LOGGER,
    MAX_CONTEXT_APPROVAL_EXAMPLES,
)
from articles_to_anki.documents.text_cache import load_or_extract_document_text
from articles_to_anki.enrichment.contexts import approve_context_candidates
from articles_to_anki.enrichment.prompts import DEFAULT_SEMANTIC_MODEL
from articles_to_anki.extraction.search import (
    document_word_index,
    find_article_contexts,
    find_variant_article_contexts,
)
from articles_to_anki.extraction.types import DocumentWordIndex
from articles_to_anki.models import ContextCandidate, TargetContext


def refresh_article_contexts(
    database: sqlite3.Connection,
    user_id: int,
    *,
    card_ids: set[str] | None = None,
    data_dir: Path | None = None,
) -> set[str]:
    """Mine uploaded PDFs for extra meaning-only contexts.

    Exact occurrences plus imprecise word-formation matches (impair -> impaired)
    are collected, then the semantic model approves only the sentences that teach
    the card's exact sense. Without an API key a conservative offline fallback
    keeps the legacy lexical-overlap selection.
    """
    parameters: list[Any] = [user_id]
    card_filter = ""
    if card_ids is not None:
        if not card_ids:
            return set()
        placeholders = ",".join("?" for _ in card_ids)
        card_filter = f" AND id IN ({placeholders})"
        parameters.extend(sorted(card_ids))
    cards = database.execute(
        f"""SELECT * FROM cards
            WHERE user_id = ? AND semantic_version = 1{card_filter}
            ORDER BY created_at""",
        parameters,
    ).fetchall()
    documents = database.execute(
        """SELECT id, user_id, name, source_path, stored_path, text_path FROM documents
           WHERE user_id = ? AND kind = 'pdf' ORDER BY created_at""",
        (user_id,),
    ).fetchall()
    parsed_documents = []
    for document in documents:
        try:
            parsed = load_or_extract_document_text(database, document)
            parsed_documents.append((document, parsed))
        except (OSError, PdfReadError, RuntimeError, TypeError, ValueError) as exc:
            LOGGER.warning(
                "Could not load article text for %s: %s", document["name"], exc
            )
            continue

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("OPENROUTER_SEMANTIC_MODEL", DEFAULT_SEMANTIC_MODEL)
    word_indexes: dict[str, DocumentWordIndex] = {}
    changed: set[str] = set()
    for card in cards:
        contexts = [
            item
            for item in json.loads(card["contexts_json"] or "[]")
            if isinstance(item, dict)
        ]
        retained = [item for item in contexts if item.get("source") != "article_context"]
        targets = {
            " ".join(str(value).casefold().split())
            for value in (
                str(card["lemma"] or ""),
                str(card["family_key"] or ""),
                *(str(item.get("target") or "") for item in retained),
            )
            if value.strip()
        }
        known_sentences = {
            " ".join(str(item.get("sentence") or "").casefold().split())
            for item in retained
        }
        candidates: list[dict[str, Any]] = []
        for document, parsed in parsed_documents:
            index = word_indexes.get(document["id"])
            if index is None:
                index = document_word_index(parsed)
                word_indexes[document["id"]] = index
            collected: list[tuple[bool, TargetContext]] = [
                (True, occurrence)
                for occurrence in find_article_contexts(parsed, sorted(targets))
            ]
            collected.extend(
                (False, occurrence)
                for occurrence in find_variant_article_contexts(
                    parsed,
                    sorted(targets),
                    index=index,
                )
            )
            for exact, occurrence in collected[:CONTEXT_CANDIDATES_PER_DOCUMENT]:
                sentence_key = " ".join(occurrence.sentence.casefold().split())
                if sentence_key in known_sentences:
                    continue
                if not _article_context_is_readable(occurrence.sentence):
                    continue
                score = _article_context_score(
                    card,
                    retained,
                    occurrence.target,
                    occurrence.sentence,
                    targets,
                )
                candidates.append(
                    _article_context_candidate(
                        card,
                        document,
                        occurrence.target,
                        occurrence.sentence,
                        occurrence.source_page,
                        occurrence.id,
                        exact=exact,
                        score=score,
                    )
                )
                known_sentences.add(sentence_key)
        if not candidates:
            continue
        approved: set[str] | None = None
        if api_key:
            approval_candidates = candidates[:CONTEXT_APPROVAL_CANDIDATE_LIMIT]
            approval_request = {
                "leader": str(card["lemma"] or card["family_key"] or card["target"]),
                "definition": str(card["sense_definition_en"] or ""),
                "translations": json.loads(card["translations_json"]),
                "known_contexts": [
                    " ".join(str(item.get("sentence") or "").split())
                    for item in retained[:MAX_CONTEXT_APPROVAL_EXAMPLES]
                    if item.get("sentence")
                ],
                "candidates": [
                    ContextCandidate(
                        id=str(item["id"]),
                        surface=str(item["target"]),
                        sentence=str(item["sentence"]),
                    )
                    for item in approval_candidates
                ],
            }
            try:
                if data_dir is not None:
                    approved = context_approval_cached(
                        _llm_cache_dir(data_dir),
                        user_id,
                        api_key=api_key,
                        model=model,
                        **approval_request,
                    )
                else:
                    approved = approve_context_candidates(
                        api_key=api_key,
                        model=model,
                        **approval_request,
                    )
            except Exception:
                LOGGER.warning(
                    "Article-context approval failed for card %s; "
                    "falling back to lexical selection",
                    card["id"],
                )
                approved = None
        selected = _select_article_context_candidates(candidates, approved)
        updated = retained + selected
        if updated == contexts:
            continue
        database.execute(
            """UPDATE cards SET contexts_json = ?, csv_exported_at = NULL,
               apkg_exported_at = NULL, anki_synced_at = NULL
               WHERE id = ? AND user_id = ?""",
            (json.dumps(updated, ensure_ascii=False), card["id"], user_id),
        )
        changed.add(str(card["id"]))
    return changed


def remove_article_contexts_for_document(
    database: sqlite3.Connection,
    user_id: int,
    document_id: str,
) -> set[str]:
    changed: set[str] = set()
    cards = database.execute(
        """SELECT id, contexts_json FROM cards
           WHERE user_id = ? AND semantic_version = 1""",
        (user_id,),
    ).fetchall()
    for card in cards:
        contexts = json.loads(card["contexts_json"] or "[]")
        retained = [
            item
            for item in contexts
            if not (
                isinstance(item, dict)
                and item.get("source") == "article_context"
                and str(item.get("document_id")) == document_id
            )
        ]
        if retained == contexts:
            continue
        database.execute(
            """UPDATE cards SET contexts_json = ?, csv_exported_at = NULL,
               apkg_exported_at = NULL, anki_synced_at = NULL
               WHERE id = ? AND user_id = ?""",
            (json.dumps(retained, ensure_ascii=False), card["id"], user_id),
        )
        changed.add(str(card["id"]))
    return changed
