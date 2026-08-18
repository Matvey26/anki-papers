from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import zstandard
from rapidfuzz import fuzz, process

from .apkg import checksum, guid, plain_text
from .enrich import DEFAULT_SEMANTIC_MODEL
from .extract import (
    DocumentText,
    DocumentWordIndex,
    document_word_index,
    find_article_contexts,
    find_variant_article_contexts,
)
from .models import (
    ClusterAnalysis,
    ClusterCandidate,
    ClusterExample,
    ContextCandidate,
)
from .webapp import (
    CLUSTER_FUZZY_SCORE_CUTOFF,
    CONTEXT_APPROVAL_CANDIDATE_LIMIT,
    CONTEXT_CANDIDATES_PER_DOCUMENT,
    MAX_CLUSTER_CANDIDATES,
    MAX_CLUSTER_EXAMPLES,
    MissingApiKeyError,
    _article_context_candidate,
    _article_context_is_readable,
    _article_context_score,
    _llm_cache_dir,
    _select_article_context_candidates,
    cluster_analysis_cached,
    context_approval_cached,
    load_or_extract_document_text,
    merge_semantic_translations,
    normalize_selected_text,
    normalize_target,
    now,
    semantic_card_rows,
)

LOGGER = logging.getLogger(__name__)

REBUILD_REVISION = "v1"
REBUILD_DECK_NAME = "Anki Papers (пересборка)"

_COLLECTION_NAMES = ("collection.anki21b", "collection.anki21", "collection.anki2")
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_TEMPLATE_JSON = Path(__file__).with_name("rebuild_template.json")

_SCHEDULE_FIELDS = (
    "type",
    "queue",
    "due",
    "ivl",
    "factor",
    "reps",
    "lapses",
    "left",
    "odue",
    "odid",
)


@dataclass(slots=True)
class HighlightEntry:
    id: str
    target: str
    sentence: str
    page: int
    document_id: str
    created_at: str


@dataclass(slots=True)
class RebuildCluster:
    id: str
    target: str
    leader: str
    sentence: str
    part_of_speech: str
    sense_definition_en: str
    translations: list[str]
    contexts: list[dict[str, Any]]
    highlight_ids: list[str]
    old_card_ids: list[str]
    source_page: int
    document_id: str


def _cluster_id(leader: str, sentence: str) -> str:
    digest = hashlib.sha256(
        f"{REBUILD_REVISION}\0{leader}\0{sentence}".encode()
    ).digest()
    return f"rebuild-{digest[:12].hex()}"


def _stable_note_id(deck_id: int, site_id: str, direction: str) -> int:
    digest = hashlib.sha256(
        f"{REBUILD_REVISION}\0{deck_id}\0{site_id}\0{direction}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


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


def rebuild_semantic_deck(
    database: sqlite3.Connection,
    user_id: int,
    *,
    data_dir: Path,
) -> dict[str, Any]:
    """Build a fresh semantic deck dataset from all highlights.

    Deterministic replay: highlights are processed in created_at order and
    joined with clusters the same way the live site does. Analyses of already
    enriched highlights are seeded from the current cards; only new highlights
    and first-time context approvals reach the model, and every LLM result is
    cached by content so rebuilds are free after the first run. The stored
    cards table is never modified.
    """
    seeds, seeds_old_cards = _collect_seeds(database, user_id)
    entries = _highlight_entries(database, user_id)
    clusters: list[RebuildCluster] = []
    highlight_to_cluster: dict[str, str] = {}

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("OPENROUTER_SEMANTIC_MODEL", DEFAULT_SEMANTIC_MODEL)

    for entry in entries:
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
    for cluster in clusters:
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


def _site_direction(tags: str) -> tuple[str, str]:
    tag_set = set(str(tags).split())
    site_tag = next(
        (tag for tag in tag_set if tag.startswith("anki_papers::")),
        "",
    )
    direction = next(
        (
            value
            for value in ("meaning", "recall")
            if f"card::{value}" in tag_set or f"direction::{value}" in tag_set
        ),
        "",
    )
    return site_tag.removeprefix("anki_papers::"), direction


def _extract_schedules(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], dict[str, int]]:
    """Read card scheduling from the old deck, keyed by site card and direction."""
    try:
        rows = connection.execute(
            """SELECT n.tags AS tags, c.type, c.queue, c.due, c.ivl, c.factor,
               c.reps, c.lapses, c.left, c.odue, c.odid
               FROM notes n JOIN cards c ON c.nid = n.id"""
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    schedules: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        site_id, direction = _site_direction(row[0])
        if not site_id or not direction:
            continue
        if (site_id, direction) in schedules:
            continue
        schedules[(site_id, direction)] = {
            key: int(row[index])
            for index, key in enumerate(_SCHEDULE_FIELDS, start=1)
        }
    return schedules


def _rebuild_deck_id(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT decks FROM col LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None:
        try:
            decks = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            decks = None
        if decks:
            return int(min(decks, key=lambda key: int(key)))
    try:
        return int(
            connection.execute("SELECT id FROM decks ORDER BY id LIMIT 1").fetchone()[0]
        )
    except (sqlite3.OperationalError, TypeError, IndexError):
        raise RuntimeError("Rebuild source contains no decks")


def _deck_name(value: datetime) -> str:
    """Deck name for one rebuild, labelled with its minute of generation.

    The minute timestamp keeps successive rebuilds distinguishable in Anki
    instead of collecting under one permanently named deck.
    """
    return f"{REBUILD_DECK_NAME} {value:%Y-%m-%d %H:%M}"


def _rename_deck(
    connection: sqlite3.Connection, deck_id: int, deck_name: str
) -> None:
    try:
        row = connection.execute("SELECT decks FROM col LIMIT 1").fetchone()
        decks = json.loads(row[0]) if row else None
    except (sqlite3.OperationalError, TypeError, ValueError, json.JSONDecodeError):
        decks = None
    if decks:
        decks[str(deck_id)]["name"] = deck_name
        connection.execute(
            "UPDATE col SET decks = ?",
            (json.dumps(decks, ensure_ascii=False),),
        )
        return
    try:
        connection.execute(
            "UPDATE decks SET name = ? WHERE id = ?", (deck_name, deck_id)
        )
    except sqlite3.OperationalError:
        raise RuntimeError("Rebuild source contains no decks")


def _rebuild_note_type_id(connection: sqlite3.Connection) -> int:
    try:
        models = json.loads(
            connection.execute("SELECT models FROM col LIMIT 1").fetchone()[0]
        )
    except (sqlite3.OperationalError, TypeError, ValueError, json.JSONDecodeError):
        models = None
    if models:
        for model_id, model in models.items():
            if len(model.get("flds", [])) == 2:
                return int(model_id)
    try:
        counts: dict[int, int] = {}
        for (ntid,) in connection.execute("SELECT ntid FROM fields"):
            counts[int(ntid)] = counts.get(int(ntid), 0) + 1
        for ntid in sorted(counts):
            if counts[ntid] == 2:
                return ntid
    except sqlite3.OperationalError:
        pass
    try:
        row = connection.execute(
            "SELECT mid FROM notes GROUP BY mid ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            return int(row[0])
    except sqlite3.OperationalError:
        pass
    raise RuntimeError("Rebuild source contains no compatible note type")


def _empty_collection(connection: sqlite3.Connection) -> None:
    for table in ("notes", "cards", "revlog", "graves"):
        try:
            connection.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            continue


def _connect_collection(path: Path) -> sqlite3.Connection:
    """Open a collection file, registering the `unicase` collation.

    Modern (Anki 2.1.50+) collections sort deck names and tags with the
    `unicase` collation; without it plain sqlite3 refuses to read those
    tables at all.
    """
    connection = sqlite3.connect(path)

    def unicase(left: str, right: str) -> int:
        normalized_left = unicodedata.normalize("NFC", left or "").casefold()
        normalized_right = unicodedata.normalize("NFC", right or "").casefold()
        return (normalized_left > normalized_right) - (
            normalized_left < normalized_right
        )

    connection.create_collation("unicase", unicase)
    return connection


def _fallback_collection(temporary: Path) -> Path:
    """Build a minimal legacy Anki collection when no old deck is available."""
    payload = json.loads(_TEMPLATE_JSON.read_text(encoding="utf-8"))
    collection = temporary / "collection.anki2"
    database = _connect_collection(collection)
    try:
        database.executescript(
            """
            CREATE TABLE col (
                id INTEGER PRIMARY KEY, crt INTEGER NOT NULL, mod INTEGER NOT NULL,
                scm INTEGER NOT NULL, ver INTEGER NOT NULL, dty INTEGER NOT NULL,
                usn INTEGER NOT NULL, ls INTEGER NOT NULL, conf TEXT NOT NULL,
                models TEXT NOT NULL, decks TEXT NOT NULL, dconf TEXT NOT NULL,
                tags TEXT NOT NULL
            );
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY, guid TEXT NOT NULL, mid INTEGER NOT NULL,
                mod INTEGER NOT NULL, usn INTEGER NOT NULL, tags TEXT NOT NULL,
                flds TEXT NOT NULL, sfld TEXT NOT NULL, csum INTEGER NOT NULL,
                flags INTEGER NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE cards (
                id INTEGER PRIMARY KEY, nid INTEGER NOT NULL, did INTEGER NOT NULL,
                ord INTEGER NOT NULL, mod INTEGER NOT NULL, usn INTEGER NOT NULL,
                type INTEGER NOT NULL, queue INTEGER NOT NULL, due INTEGER NOT NULL,
                ivl INTEGER NOT NULL, factor INTEGER NOT NULL, reps INTEGER NOT NULL,
                lapses INTEGER NOT NULL, left INTEGER NOT NULL, odue INTEGER NOT NULL,
                odid INTEGER NOT NULL, flags INTEGER NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE revlog (
                id INTEGER PRIMARY KEY, cid INTEGER NOT NULL, usn INTEGER NOT NULL,
                ease INTEGER NOT NULL, ivl INTEGER NOT NULL, lastIvl INTEGER NOT NULL,
                factor INTEGER NOT NULL, time INTEGER NOT NULL, type INTEGER NOT NULL
            );
            CREATE TABLE graves (
                id INTEGER PRIMARY KEY, oid INTEGER NOT NULL, type INTEGER NOT NULL,
                usn INTEGER NOT NULL
            );
            """
        )
        mod = int(time.time())
        database.execute(
            "INSERT INTO col VALUES (1, ?, ?, ?, ?, 0, -1, 0, ?, ?, ?, ?, ?)",
            (
                payload["crt"],
                mod,
                mod * 1000,
                payload["ver"],
                json.dumps(payload["conf"], ensure_ascii=False),
                json.dumps(payload["models"], ensure_ascii=False),
                json.dumps(payload["decks"], ensure_ascii=False),
                json.dumps(payload["dconf"], ensure_ascii=False),
                json.dumps(payload["tags"], ensure_ascii=False),
            ),
        )
        database.commit()
    finally:
        database.close()
    return collection


def _open_old_collection(
    source_path: Path | None,
    temporary: Path,
) -> tuple[Path, bool]:
    """Return a plain sqlite path for the old deck and whether it was zstd."""
    if source_path is None:
        return _fallback_collection(temporary), False
    head = source_path.read_bytes()[:2]
    if head == b"PK":
        with zipfile.ZipFile(source_path) as archive:
            for member in archive.infolist():
                member_path = PurePosixPath(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError("APKG contains an unsafe path.")
            archive.extractall(temporary)
        collection = next(
            (
                temporary / name
                for name in _COLLECTION_NAMES
                if (temporary / name).is_file()
            ),
            None,
        )
        if collection is None:
            raise RuntimeError("APKG does not contain a supported Anki collection.")
    else:
        collection = temporary / "collection.anki2"
        shutil.copyfile(source_path, collection)
    if collection.read_bytes()[:4] == _ZSTD_MAGIC:
        database = temporary / "collection.sqlite"
        with collection.open("rb") as source_stream, database.open(
            "wb"
        ) as database_stream:
            zstandard.ZstdDecompressor().copy_stream(source_stream, database_stream)
        return database, True
    return collection, False


def build_rebuilt_deck_apkg(
    database: sqlite3.Connection,
    user_id: int,
    *,
    data_dir: Path,
    source_path: Path | None = None,
) -> bytes:
    """Rebuild all highlights into a fresh APKG with carried-over schedules.

    The rebuilt deck contains one two-field note per semantic card (meaning
    and recall rows), tagged with the same `anki_papers::<card id>` site tags
    as the live deck. When an old collection is available (upload or mirror),
    card states from matching old notes are copied across so learning progress
    survives; otherwise new cards start fresh. The stored data is never
    modified.
    """
    replay = rebuild_semantic_deck(database, user_id, data_dir=data_dir)
    cards = replay["cards"]
    if not cards:
        raise RuntimeError("Нет сохранённых слов для пересобранной колоды")
    rows: list[dict[str, str]] = []
    for card in cards:
        try:
            rows.extend(semantic_card_rows(card))
        except (KeyError, TypeError, ValueError):
            LOGGER.warning("Rebuild: skipping card %s without usable contexts", card["id"])
    if not rows:
        raise RuntimeError("Нет карточек для пересобранной колоды")

    now_seconds = int(time.time())
    deck_name = _deck_name(datetime.fromtimestamp(now_seconds, tz=UTC))
    with tempfile.TemporaryDirectory(prefix="anki-papers-rebuild-") as temporary_name:
        temporary = Path(temporary_name)
        collection_path, is_compressed = _open_old_collection(source_path, temporary)
        connection = _connect_collection(collection_path)
        try:
            schedules = _extract_schedules(connection)
            deck_id = _rebuild_deck_id(connection)
            _rename_deck(connection, deck_id, deck_name)
            note_type_id = _rebuild_note_type_id(connection)
            _empty_collection(connection)
            next_due = 1
            for index, row in enumerate(rows):
                site_id, direction = _site_direction(row["Tags"])
                note_id = _stable_note_id(deck_id, site_id, direction)
                state = schedules.get((site_id, direction))
                if state is not None:
                    kind, queue, due, ivl, factor, reps, lapses, left, odue, odid = (
                        state[field] for field in _SCHEDULE_FIELDS
                    )
                else:
                    due = next_due + index
                    kind, queue, ivl, factor, reps, lapses, left, odue, odid = (
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                    )
                front = row["Front"]
                back = row["Back"]
                fields = front + "\x1f" + back
                tags = " " + " ".join(row["Tags"].split()) + " "
                connection.execute(
                    "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        note_id,
                        guid(front),
                        note_type_id,
                        now_seconds,
                        -1,
                        tags,
                        fields,
                        plain_text(front),
                        checksum(front),
                        0,
                        "",
                    ),
                )
                connection.execute(
                    "INSERT INTO cards VALUES (?, ?, ?, 0, ?, -1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '{}')",
                    (
                        note_id,
                        note_id,
                        deck_id,
                        now_seconds,
                        kind,
                        queue,
                        due,
                        ivl,
                        factor,
                        reps,
                        lapses,
                        left,
                        odue,
                        odid,
                    ),
                )
            try:
                connection.execute(
                    "UPDATE config SET val = ?, usn = -1, mtime_secs = ? WHERE key = 'nextPos'",
                    (str(next_due + len(rows)), now_seconds),
                )
            except sqlite3.OperationalError:
                pass
            connection.execute("UPDATE col SET mod = ?", (now_seconds * 1000,))
            connection.commit()
        finally:
            connection.close()

        collection_name = "collection.anki21b" if is_compressed else "collection.anki2"
        if is_compressed:
            with collection_path.open("rb") as database_stream, (
                temporary / collection_name
            ).open("wb") as collection_stream:
                zstandard.ZstdCompressor(level=10).copy_stream(
                    database_stream, collection_stream
                )
            collection_path.unlink()
        media = temporary / "media"
        media.write_text("{}")
        members = [
            path
            for path in sorted(temporary.iterdir())
            if path.name == collection_name or path.name == "media"
        ]
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for member in members:
                info = zipfile.ZipInfo(member.name)
                archive.writestr(info, member.read_bytes())
    return stream.getvalue()