"""cards / clusters."""
from __future__ import annotations

import json
import sqlite3

from rapidfuzz import fuzz, process

from articles_to_anki.cards.text import normalize_selected_text
from articles_to_anki.constants import (
    CLUSTER_FUZZY_SCORE_CUTOFF,
    MAX_CLUSTER_CANDIDATES,
    MAX_CLUSTER_EXAMPLES,
)
from articles_to_anki.models import ClusterCandidate, ClusterExample


class MissingApiKeyError(RuntimeError):
    pass


def cluster_examples(
    database: sqlite3.Connection,
    card: sqlite3.Row,
) -> list[ClusterExample]:
    rows = database.execute(
        """SELECT highlights.target, highlights.sentence
           FROM highlights
           JOIN card_highlights ON card_highlights.highlight_id = highlights.id
           WHERE card_highlights.card_id = ? AND highlights.user_id = ?
           ORDER BY highlights.created_at""",
        (card["id"], card["user_id"]),
    ).fetchall()
    values: list[ClusterExample] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        highlight = normalize_selected_text(str(row["target"]))
        context = " ".join(str(row["sentence"]).split())
        key = (highlight, context.casefold())
        if not highlight or not context or key in seen:
            continue
        seen.add(key)
        values.append(ClusterExample(highlight=highlight, context=context))
        if len(values) == MAX_CLUSTER_EXAMPLES:
            return values

    for item in json.loads(card["contexts_json"] or "[]"):
        if not isinstance(item, dict) or item.get("source") != "user_pdf":
            continue
        highlight = normalize_selected_text(str(item.get("target") or ""))
        context = " ".join(str(item.get("sentence") or "").split())
        key = (highlight, context.casefold())
        if not highlight or not context or key in seen:
            continue
        seen.add(key)
        values.append(ClusterExample(highlight=highlight, context=context))
        if len(values) == MAX_CLUSTER_EXAMPLES:
            return values

    if not values:
        values.append(
            ClusterExample(
                highlight=normalize_selected_text(str(card["target"])),
                context=" ".join(str(card["sentence"]).split()),
            )
        )
    return values


def find_cluster_candidates(
    database: sqlite3.Connection,
    user_id: int,
    normalized_highlight: str,
    *,
    limit: int = MAX_CLUSTER_CANDIDATES,
) -> list[ClusterCandidate]:
    cards = database.execute(
        """SELECT * FROM cards
           WHERE user_id = ? AND semantic_version = 1
           ORDER BY created_at, id""",
        (user_id,),
    ).fetchall()
    leaders = {
        str(card["id"]): normalize_selected_text(
            str(card["target"] or card["family_key"] or card["lemma"] or "")
        )
        for card in cards
    }
    leaders = {cluster_id: leader for cluster_id, leader in leaders.items() if leader}
    matches = process.extract(
        normalized_highlight,
        leaders,
        scorer=fuzz.WRatio,
        processor=None,
        score_cutoff=CLUSTER_FUZZY_SCORE_CUTOFF,
        limit=max(1, min(limit, MAX_CLUSTER_CANDIDATES)),
    )
    cards_by_id = {str(card["id"]): card for card in cards}
    return [
        ClusterCandidate(
            cluster_id=str(cluster_id),
            leader=leader,
            examples=cluster_examples(database, cards_by_id[str(cluster_id)]),
        )
        for leader, _score, cluster_id in matches
    ]


def merge_semantic_translations(*groups: list[str], limit: int = 8) -> list[str]:
    merged: list[str] = []
    normalized: set[str] = set()
    for group in groups:
        for value in group:
            cleaned = value.strip()
            key = cleaned.casefold()
            if cleaned and key not in normalized:
                merged.append(cleaned)
                normalized.add(key)
            if len(merged) == limit:
                return merged
    return merged
