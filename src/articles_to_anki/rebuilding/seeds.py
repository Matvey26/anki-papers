"""rebuilding / seeds."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from rapidfuzz import fuzz, process

from articles_to_anki.cards.text import normalize_selected_text
from articles_to_anki.constants import (
    CLUSTER_FUZZY_SCORE_CUTOFF,
    MAX_CLUSTER_CANDIDATES,
    MAX_CLUSTER_EXAMPLES,
)
from articles_to_anki.models import ClusterCandidate, ClusterExample
from articles_to_anki.rebuilding.types import HighlightEntry, RebuildCluster


def _cluster_candidate_models(clusters: list[RebuildCluster]) -> list[ClusterCandidate]:
    candidates: list[ClusterCandidate] = []
    for cluster in clusters:
        if not cluster.leader:
            continue
        examples: list[ClusterExample] = []
        for item in cluster.contexts[:MAX_CLUSTER_EXAMPLES]:
            highlight = normalize_selected_text(str(item.get("target") or ""))
            context = " ".join(str(item.get("sentence") or "").split())
            if not highlight or not context:
                continue
            examples.append(ClusterExample(highlight=highlight, context=context))
        if not examples:
            continue
        candidates.append(
            ClusterCandidate(
                cluster_id=cluster.id,
                leader=cluster.leader,
                examples=examples,
            )
        )
    return candidates


def _cluster_candidates(
    clusters: list[RebuildCluster],
    normalized_highlight: str,
) -> list[RebuildCluster]:
    leaders = {
        cluster.id: normalize_selected_text(cluster.leader)
        for cluster in clusters
    }
    leaders = {cluster_id: leader for cluster_id, leader in leaders.items() if leader}
    if not leaders:
        return []
    matches = process.extract(
        normalized_highlight,
        leaders,
        scorer=fuzz.WRatio,
        processor=None,
        score_cutoff=CLUSTER_FUZZY_SCORE_CUTOFF,
        limit=MAX_CLUSTER_CANDIDATES,
    )
    matches.sort(key=lambda match: (-match[1], match[2]))
    by_id = {cluster.id: cluster for cluster in clusters}
    return [by_id[cluster_id] for _leader, _score, cluster_id in matches]


def seed_analysis_from_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "cluster_id": "new_cluster",
        "leader": str(context.get("lemma") or context.get("target") or ""),
        "part_of_speech": str(context.get("part_of_speech") or ""),
        "cluster_definition_en": str(context.get("sense_definition_en") or ""),
        "translations_ru": list(context.get("translations") or []),
        "replacement_ru": str(context.get("replacement") or ""),
        "source_distractors": {
            "substitutes_en": list(context.get("substitutes_en") or []),
            "related_en": list(context.get("related_en") or []),
            "valid_substitutes_en": list(context.get("valid_substitutes_en") or []),
            "valid_related_en": list(context.get("valid_related_en") or []),
        },
    }


def _collect_seeds(
    database: sqlite3.Connection,
    user_id: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Reuse analyses already stored on semantic cards, keyed by highlight id.

    Highlights are linked to their cards through `card_highlights`; a context
    stored on that card (whatever its source label, including `user_pdf`,
    `reader` and `pdf_import`) is a seed, so highlights that already went
    through the live pipeline never reach the model again. Contexts whose id
    directly names an existing highlight are also seeds.
    """
    seeds: dict[str, dict[str, Any]] = {}
    seeds_old_cards: dict[str, str] = {}
    highlight_ids = {
        str(row[0])
        for row in database.execute(
            "SELECT id FROM highlights WHERE user_id = ?", (user_id,)
        )
    }
    rows = database.execute(
        """SELECT id, contexts_json FROM cards
           WHERE user_id = ? AND semantic_version = 1""",
        (user_id,),
    ).fetchall()
    highlight_to_card: dict[str, str] = {}
    try:
        highlight_to_card = {
            str(row[0]): str(row[1])
            for row in database.execute(
                """SELECT highlight_id, card_id FROM card_highlights
                   JOIN cards ON cards.id = card_id
                   WHERE cards.user_id = ? AND cards.semantic_version = 1""",
                (user_id,),
            )
        }
    except sqlite3.OperationalError:
        pass
    for row in rows:
        card_id = str(row["id"])
        try:
            contexts = json.loads(row["contexts_json"] or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(contexts, list):
            continue
        for context in contexts:
            if not isinstance(context, dict):
                continue
            highlight_id = str(context.get("id") or "")
            if highlight_id in seeds:
                continue
            if highlight_id in highlight_to_card:
                if highlight_to_card[highlight_id] != card_id:
                    continue
            elif highlight_id not in highlight_ids:
                continue
            seeds[highlight_id] = seed_analysis_from_context(context)
            seeds_old_cards[highlight_id] = card_id
    return seeds, seeds_old_cards


def _highlight_entries(
    database: sqlite3.Connection,
    user_id: int,
) -> list[HighlightEntry]:
    rows = database.execute(
        """SELECT id, document_id, target, sentence, page, created_at
           FROM highlights
           WHERE user_id = ? AND status = 'ready'
           ORDER BY created_at, id""",
        (user_id,),
    ).fetchall()
    return [
        HighlightEntry(
            id=str(row["id"]),
            target=str(row["target"]),
            sentence=str(row["sentence"]),
            page=int(row["page"]),
            document_id=str(row["document_id"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]
