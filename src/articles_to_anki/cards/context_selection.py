"""cards / context_selection."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Any

from articles_to_anki.constants import _ARTICLE_STOPWORDS, ARTICLE_CONTEXT_LIMIT


def _article_words(value: str, *, targets: set[str]) -> set[str]:
    target_words = {
        token
        for target in targets
        for token in re.findall(r"[a-z]+", target.casefold())
    }
    return {
        token
        for token in re.findall(r"[a-z]+", value.casefold())
        if len(token) >= 3
        and token not in _ARTICLE_STOPWORDS
        and token not in target_words
    }


def _article_context_score(
    card: sqlite3.Row,
    contexts: list[dict[str, Any]],
    target: str,
    sentence: str,
    targets: set[str],
) -> int | None:
    if not _article_context_is_readable(sentence):
        return None
    target_word_count = len(re.findall(r"[A-Za-z]+", target))
    anchor_text = " ".join(
        [str(card["sense_definition_en"])]
        + [
            str(item.get("sentence") or "")
            for item in contexts
            if item.get("source") not in {"article_context", "llm_generated"}
        ]
    )
    anchor_words = _article_words(anchor_text, targets=targets)
    candidate_words = _article_words(sentence, targets=targets)
    overlap = len(anchor_words & candidate_words)
    if target_word_count >= 2:
        return 100 + overlap
    if overlap < 2:
        return None
    return overlap


def _article_context_is_readable(sentence: str) -> bool:
    cleaned = sentence.strip()
    if len(cleaned) < 45 or cleaned.endswith(" .") or "|" in cleaned:
        return False
    if re.match(r"^(?:…\s*)?\d+\s+\d+(?:\.\d+)+\s+", cleaned):
        return False
    numeric_tokens = re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?%?", cleaned)
    percentages = [value for value in numeric_tokens if value.endswith("%")]
    if len(numeric_tokens) >= 8 or len(percentages) >= 3:
        return False
    if cleaned.startswith("…") and len(numeric_tokens) >= 4:
        return False
    if sum(unicodedata.category(char).startswith("S") for char in cleaned) >= 5:
        return False
    return True


def _article_context_candidate(
    card: sqlite3.Row,
    document: sqlite3.Row,
    target: str,
    sentence: str,
    page: int,
    occurrence_id: str,
    *,
    exact: bool,
    score: int | None,
) -> dict[str, Any]:
    return {
        "id": f"article:{document['id']}:{occurrence_id}",
        "source": "article_context",
        "document_id": str(document["id"]),
        "document_name": str(document["name"]),
        "page": page,
        "directions": ["meaning"],
        "target": target,
        "sentence": sentence,
        "replacement": str(card["replacement"]),
        "lemma": str(card["lemma"]),
        "family_key": str(card["family_key"]),
        "part_of_speech": str(card["part_of_speech"]),
        "sense_definition_en": str(card["sense_definition_en"]),
        "score": score,
        "exact": exact,
    }


def _select_article_context_candidates(
    candidates: list[dict[str, Any]],
    approved: set[str] | None,
) -> list[dict[str, Any]]:
    def order_key(item: dict[str, Any]) -> tuple[Any, ...]:
        if approved is not None:
            return (
                item["id"] not in approved,
                not item["exact"],
                -(item["score"] or 0),
                item["document_id"],
                item["page"],
                item["id"],
            )
        score = item["score"]
        return (
            -score if score is not None else -1,
            item["document_id"],
            item["page"],
            item["id"],
        )

    if approved is None:
        candidates = [item for item in candidates if item["score"] is not None]
    selected = []
    per_document: dict[str, int] = {}
    for candidate in sorted(candidates, key=order_key):
        if per_document.get(candidate["document_id"], 0) >= 2:
            continue
        selected.append(candidate)
        per_document[candidate["document_id"]] = (
            per_document.get(candidate["document_id"], 0) + 1
        )
        if len(selected) == ARTICLE_CONTEXT_LIMIT:
            break
    return selected
