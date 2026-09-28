"""rebuilding / contexts."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

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
)
from articles_to_anki.documents.text_cache import load_or_extract_document_text
from articles_to_anki.extraction.search import (
    document_word_index,
    find_article_contexts,
    find_variant_article_contexts,
)
from articles_to_anki.extraction.types import DocumentText, DocumentWordIndex
from articles_to_anki.models import ContextCandidate
from articles_to_anki.rebuilding.constants import LOGGER
from articles_to_anki.rebuilding.types import RebuildCluster


def _docs_and_indexes(
    database: sqlite3.Connection,
    user_id: int,
) -> tuple[list[tuple[dict[str, Any], DocumentText]], dict[str, DocumentWordIndex]]:
    documents = database.execute(
        """SELECT id, user_id, name, source_path, stored_path, text_path
           FROM documents WHERE user_id = ? AND kind = 'pdf' ORDER BY created_at""",
        (user_id,),
    ).fetchall()
    parsed_documents: list[tuple[dict[str, Any], DocumentText]] = []
    for document in documents:
        try:
            parsed = load_or_extract_document_text(database, document)
            parsed_documents.append((dict(document), parsed))
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            LOGGER.warning(
                "Rebuild: could not load article text for %s: %s",
                document["name"],
                exc,
            )
            continue
    indexes = {
        document["id"]: document_word_index(parsed)
        for document, parsed in parsed_documents
    }
    return parsed_documents, indexes


def _old_mined_contexts(
    database: sqlite3.Connection,
    user_id: int,
) -> dict[str, list[dict[str, Any]]]:
    by_card: dict[str, list[dict[str, Any]]] = {}
    rows = database.execute(
        """SELECT id, contexts_json FROM cards
           WHERE user_id = ? AND semantic_version = 1""",
        (user_id,),
    ).fetchall()
    for row in rows:
        try:
            contexts = json.loads(row["contexts_json"] or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(contexts, list):
            continue
        for item in contexts:
            if isinstance(item, dict) and item.get("source") == "article_context":
                by_card.setdefault(str(row["id"]), []).append(item)
    return by_card


def _card_view(cluster: RebuildCluster) -> dict[str, Any]:
    first_context = next(iter(cluster.contexts), None)
    return {
        "replacement": (
            str(first_context.get("replacement") or cluster.translations[0])
            if first_context
            else str(cluster.translations[0])
        ),
        "lemma": cluster.leader,
        "family_key": cluster.leader,
        "part_of_speech": cluster.part_of_speech,
        "sense_definition_en": cluster.sense_definition_en,
    }


def _mined_contexts_for_cluster(
    cluster: RebuildCluster,
    parsed_documents: list[tuple[dict[str, Any], DocumentText]],
    indexes: dict[str, DocumentWordIndex],
    *,
    data_dir: Path,
    user_id: int,
    api_key: str,
    model: str,
) -> list[dict[str, Any]]:
    retained = [
        item for item in cluster.contexts if item.get("source") != "article_context"
    ]
    targets = {
        " ".join(str(cluster.leader).casefold().split()),
        *(
            " ".join(str(item.get("target") or "").casefold().split())
            for item in retained
        ),
    }
    targets = {value for value in targets if value}
    known_sentences = {
        " ".join(str(item.get("sentence") or "").casefold().split())
        for item in retained
    }
    candidates: list[dict[str, Any]] = []
    for document, parsed in parsed_documents:
        collected: list[tuple[bool, Any]] = [
            (True, occurrence)
            for occurrence in find_article_contexts(parsed, sorted(targets))
        ]
        collected.extend(
            (False, occurrence)
            for occurrence in find_variant_article_contexts(
                parsed,
                sorted(targets),
                index=indexes[document["id"]],
            )
        )
        for exact, occurrence in collected[:CONTEXT_CANDIDATES_PER_DOCUMENT]:
            sentence_key = " ".join(occurrence.sentence.casefold().split())
            if sentence_key in known_sentences:
                continue
            if not _article_context_is_readable(occurrence.sentence):
                continue
            score = _article_context_score(
                _card_view(cluster),
                retained,
                occurrence.target,
                occurrence.sentence,
                targets,
            )
            candidates.append(
                _article_context_candidate(
                    _card_view(cluster),
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
        return []
    approval_candidates = candidates[:CONTEXT_APPROVAL_CANDIDATE_LIMIT]
    approved: set[str] | None = None
    try:
        approved = context_approval_cached(
            _llm_cache_dir(data_dir),
            user_id,
            model=model,
            leader=str(cluster.leader),
            definition=str(cluster.sense_definition_en),
            translations=cluster.translations,
            known_contexts=[
                " ".join(str(item.get("sentence") or "").split())
                for item in retained[:5]
                if item.get("sentence")
            ],
            candidates=[
                ContextCandidate(
                    id=str(item["id"]),
                    surface=str(item["target"]),
                    sentence=str(item["sentence"]),
                )
                for item in approval_candidates
            ],
            api_key=api_key,
        )
    except Exception:  # noqa: BLE001 - approval failure falls back to lexical selection
        LOGGER.warning(
            "Rebuild: context approval failed for %s; using lexical selection",
            cluster.id,
        )
        approved = None
    return _select_article_context_candidates(candidates, approved)
