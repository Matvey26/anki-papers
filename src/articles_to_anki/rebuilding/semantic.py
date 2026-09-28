"""rebuilding / semantic."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from articles_to_anki.cards.clusters import (
    MissingApiKeyError,
    merge_semantic_translations,
)
from articles_to_anki.cards.llm_cache import _llm_cache_dir, cluster_analysis_cached
from articles_to_anki.cards.text import normalize_selected_text, normalize_target, now
from articles_to_anki.enrichment.prompts import DEFAULT_SEMANTIC_MODEL
from articles_to_anki.models import ClusterAnalysis
from articles_to_anki.rebuilding.constants import LOGGER, ProgressReporter
from articles_to_anki.rebuilding.contexts import (
    _docs_and_indexes,
    _mined_contexts_for_cluster,
    _old_mined_contexts,
)
from articles_to_anki.rebuilding.identity import _cluster_id
from articles_to_anki.rebuilding.seeds import (
    _cluster_candidate_models,
    _cluster_candidates,
    _collect_seeds,
    _highlight_entries,
)
from articles_to_anki.rebuilding.types import RebuildCluster


def rebuild_semantic_deck(
    database: sqlite3.Connection,
    user_id: int,
    *,
    data_dir: Path,
    progress: ProgressReporter | None = None,
) -> dict[str, Any]:
    """Build a fresh semantic deck dataset from all highlights.

    Deterministic replay: highlights are processed in created_at order and
    joined with clusters the same way the live site does. Analyses of already
    enriched highlights are seeded from the current cards; only new highlights
    and first-time context approvals reach the model, and every LLM result is
    cached by content so rebuilds are free after the first run. The stored
    cards table is never modified.
    """
    if progress is None:
        progress = lambda _percent, _stage: None
    progress(2, "Собираю данные")
    seeds, seeds_old_cards = _collect_seeds(database, user_id)
    entries = _highlight_entries(database, user_id)
    clusters: list[RebuildCluster] = []
    highlight_to_cluster: dict[str, str] = {}

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("OPENROUTER_SEMANTIC_MODEL", DEFAULT_SEMANTIC_MODEL)

    entry_count = max(len(entries), 1)
    for entry_index, entry in enumerate(entries):
        progress(
            3 + 47 * entry_index // entry_count,
            f"Слова и кластеры · {entry_index + 1}/{len(entries)}",
        )
        normalized_highlight = normalize_selected_text(entry.target)
        candidates = _cluster_candidates(clusters, normalized_highlight)
        analysis_data = seeds.get(entry.id)
        if analysis_data is None:
            try:
                cached, _cached = cluster_analysis_cached(
                    _llm_cache_dir(data_dir),
                    user_id,
                    model=model,
                    target=entry.target,
                    normalized_target=normalized_highlight,
                    sentence=entry.sentence,
                    candidates=_cluster_candidate_models(clusters),
                    api_key=api_key,
                )
                analysis_data = cached.model_dump()
            except MissingApiKeyError:
                continue
            except Exception:
                LOGGER.exception("Rebuild: cluster analysis failed for %s", entry.id)
                continue
        if not analysis_data:
            continue
        try:
            analysis = ClusterAnalysis.model_validate(analysis_data)
        except Exception:
            continue
        leader = str(analysis.leader or entry.target)
        cluster = next(
            (
                item
                for item in candidates
                if item.leader.casefold() == leader.casefold()
            ),
            None,
        )
        if cluster is None:
            old_card_id = seeds_old_cards.get(entry.id)
            cluster_id = old_card_id or _cluster_id(leader, entry.sentence)
            cluster = next(
                (item for item in clusters if item.id == cluster_id),
                None,
            )
            if cluster is None:
                cluster = RebuildCluster(
                    id=cluster_id,
                    target=entry.target,
                    leader=leader,
                    sentence=entry.sentence,
                    part_of_speech=analysis.part_of_speech.casefold(),
                    sense_definition_en=analysis.cluster_definition_en,
                    translations=list(analysis.translations_ru),
                    contexts=[],
                    highlight_ids=[],
                    old_card_ids=[old_card_id] if old_card_id else [],
                    source_page=entry.page,
                    document_id=entry.document_id,
                )
                clusters.append(cluster)
        source_context = {
            "id": entry.id,
            "source": "user_pdf",
            "target": entry.target,
            "sentence": entry.sentence,
            "replacement": analysis.replacement_ru,
            "translations": list(analysis.translations_ru),
            "lemma": leader,
            "family_key": leader,
            "part_of_speech": analysis.part_of_speech,
            "sense_definition_en": analysis.cluster_definition_en,
            "substitutes_en": list(analysis.source_distractors.substitutes_en),
            "related_en": list(analysis.source_distractors.related_en),
            "valid_substitutes_en": list(
                analysis.source_distractors.valid_substitutes_en
            ),
            "valid_related_en": list(analysis.source_distractors.valid_related_en),
        }
        cluster.translations = merge_semantic_translations(
            cluster.translations,
            analysis.translations_ru,
        )
        known_sentences = {
            " ".join(str(item.get("sentence") or "").casefold().split())
            for item in cluster.contexts
            if item.get("sentence")
        }
        sentence_key = " ".join(entry.sentence.casefold().split())
        if sentence_key not in known_sentences:
            cluster.contexts.append(source_context)
        cluster.highlight_ids.append(entry.id)
        highlight_to_cluster[entry.id] = cluster.id

    parsed_documents, indexes = _docs_and_indexes(database, user_id)
    old_mined = _old_mined_contexts(database, user_id)
    cluster_count = max(len(clusters), 1)
    for cluster_index, cluster in enumerate(clusters):
        progress(
            50 + 35 * cluster_index // cluster_count,
            f"Контексты предложений · {cluster_index + 1}/{len(clusters)}",
        )
        known_sentences = {
            " ".join(str(item.get("sentence") or "").casefold().split())
            for item in cluster.contexts
        }
        for old_card_id in cluster.old_card_ids:
            for item in old_mined.get(old_card_id, []):
                sentence_key = " ".join(
                    str(item.get("sentence") or "").casefold().split()
                )
                if sentence_key in known_sentences:
                    continue
                cluster.contexts.append(dict(item))
                known_sentences.add(sentence_key)
        cluster.contexts.extend(
            _mined_contexts_for_cluster(
                cluster,
                parsed_documents,
                indexes,
                data_dir=data_dir,
                user_id=user_id,
                api_key=api_key,
                model=model,
            )
        )

    cards: list[dict[str, Any]] = []
    card_map: dict[str, str] = {}
    for cluster in clusters:
        translation = cluster.translations[0] if cluster.translations else ""
        replacement = (
            str(cluster.contexts[0].get("replacement") or translation)
            if cluster.contexts
            else translation
        )
        card = {
            "id": cluster.id,
            "target": cluster.target or cluster.leader,
            "target_normalized": normalize_target(cluster.target or cluster.leader),
            "sentence": (
                cluster.contexts[0]["sentence"] if cluster.contexts else cluster.leader
            ),
            "page": cluster.source_page,
            "document_id": cluster.document_id,
            "translations_json": json.dumps(
                cluster.translations, ensure_ascii=False
            ),
            "replacement": replacement,
            "alternatives_json": "[]",
            "lemma": cluster.leader,
            "family_key": cluster.leader,
            "part_of_speech": cluster.part_of_speech,
            "sense_definition_en": cluster.sense_definition_en,
            "contexts_json": json.dumps(cluster.contexts, ensure_ascii=False),
            "semantic_version": 1,
            "created_at": now(),
        }
        cards.append(card)
        if cluster.old_card_ids:
            card_map[cluster.id] = cluster.old_card_ids[0]
    return {"cards": cards, "card_map": card_map}
