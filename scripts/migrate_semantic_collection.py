from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anki.collection import Collection
from anki_papers_sync_worker.official import OfficialAnkiAdapter, _semantic_sides

from articles_to_anki.webapp import normalize_target

UNKNOWN_DECK_NAME = "Anki Papers — неизвестные до миграции"


@dataclass(frozen=True)
class HighlightRef:
    id: str
    target: str
    sentence: str
    document_id: str
    page: int
    created_at: str
    source: str


@dataclass(frozen=True)
class SourceContext:
    id: str
    target: str
    sentence: str
    translations: list[str]
    replacement: str
    source: str
    note_ids: list[int]
    learned_cards: int


def normalized(value: str) -> str:
    cleaned = " ".join(value.casefold().split())
    cleaned = re.sub(r"\s+([,.;:!?%)\]])", r"\1", cleaned)
    return re.sub(r"([(\[])\s+", r"\1", cleaned)


def now() -> str:
    return datetime.now(UTC).isoformat()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def source_contexts(path: Path) -> list[SourceContext]:
    return [SourceContext(**item) for item in load_json(path)]


def note_direction(tags: list[str]) -> str | None:
    for tag in tags:
        for prefix in ("direction::", "card::"):
            if tag.startswith(prefix) and tag.removeprefix(prefix) in {
                "meaning",
                "recall",
            }:
                return tag.removeprefix(prefix)
    return None


def schedule_rank(card: Any) -> tuple[int, int, int, int, int]:
    return (
        int(card.type == 2),
        int(card.ivl),
        int(card.reps),
        -int(card.lapses),
        -int(card.id),
    )


def safe_family_tag(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_:-]+", "_", value).strip("_") or "word"


def clean_managed_tags(tags: list[str]) -> list[str]:
    prefixes = ("anki_papers::", "direction::", "card::", "family::")
    return [
        tag
        for tag in tags
        if not tag.startswith(prefixes) and tag != "semantic::v1"
    ]


def build_highlight_mapping(
    database: sqlite3.Connection,
    *,
    user_id: int,
    sources: list[SourceContext],
) -> tuple[dict[str, HighlightRef], dict[str, str], list[str]]:
    highlights = {
        str(row["id"]): HighlightRef(
            id=str(row["id"]),
            target=str(row["target"]),
            sentence=str(row["sentence"]),
            document_id=str(row["document_id"]),
            page=int(row["page"]),
            created_at=str(row["created_at"]),
            source=str(row["source"]),
        )
        for row in database.execute(
            "SELECT * FROM highlights WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        )
    }
    exact: dict[tuple[str, str], list[str]] = defaultdict(list)
    for highlight in highlights.values():
        exact[(normalized(highlight.target), normalized(highlight.sentence))].append(
            highlight.id
        )

    old_card_highlights: dict[str, list[str]] = defaultdict(list)
    for row in database.execute(
        """SELECT card_highlights.card_id, card_highlights.highlight_id
           FROM card_highlights
           JOIN highlights ON highlights.id = card_highlights.highlight_id
           WHERE highlights.user_id = ?""",
        (user_id,),
    ):
        old_card_highlights[str(row["card_id"])].append(str(row["highlight_id"]))
    linked_highlights: dict[int, list[str]] = defaultdict(list)
    for row in database.execute(
        "SELECT site_card_id, note_id FROM anki_note_links WHERE user_id = ?",
        (user_id,),
    ):
        linked_highlights[int(row["note_id"])].extend(
            old_card_highlights.get(str(row["site_card_id"]), [])
        )

    candidates: dict[str, set[str]] = defaultdict(set)
    for source in sources:
        key = (normalized(source.target), normalized(source.sentence))
        candidates[source.id].update(exact.get(key, []))
        for note_id in source.note_ids:
            candidates[source.id].update(linked_highlights.get(note_id, []))

    chosen: dict[str, str] = {}
    for highlight in highlights.values():
        options = [source for source in sources if highlight.id in candidates[source.id]]
        if not options:
            raise RuntimeError(
                f"No source context matches highlight {highlight.id}: {highlight.target}"
            )
        highlight_key = (normalized(highlight.target), normalized(highlight.sentence))
        options.sort(
            key=lambda source: (
                (normalized(source.target), normalized(source.sentence))
                == highlight_key,
                -min(source.note_ids),
            ),
            reverse=True,
        )
        selected = options[0]
        chosen[highlight.id] = selected.id

    if len(chosen) != len(highlights):
        raise RuntimeError("Not every live highlight received one source context")
    candidate_source_ids = {
        source.id for source in sources if candidates[source.id]
    }
    shadowed = candidate_source_ids - set(chosen.values())
    return highlights, chosen, sorted(shadowed)


def semantic_card_payload(
    cluster: dict[str, Any],
    *,
    highlights: dict[str, HighlightRef],
    chosen_by_highlight: dict[str, str],
    shadowed: set[str],
) -> tuple[dict[str, Any], list[HighlightRef]]:
    highlight_by_source = {
        source_id: highlights[highlight_id]
        for highlight_id, source_id in chosen_by_highlight.items()
    }
    contexts: list[dict[str, Any]] = []
    for context in cluster["contexts"]:
        context_id = str(context["id"])
        source_id = context_id.removeprefix("generated:")
        if source_id in shadowed:
            continue
        rewritten = dict(context)
        highlight = highlight_by_source.get(source_id)
        if highlight is not None:
            if context_id.startswith("generated:"):
                rewritten["id"] = f"generated:{highlight.id}"
            else:
                rewritten.update(
                    {
                        "id": highlight.id,
                        "source": highlight.source,
                        "target": highlight.target,
                        "sentence": highlight.sentence,
                    }
                )
        contexts.append(rewritten)
    live = [
        highlights[highlight_id]
        for highlight_id, source_id in chosen_by_highlight.items()
        if source_id in set(cluster["source_context_ids"])
    ]
    return (
        {
            "id": str(cluster["id"]),
            "semantic": True,
            "lemma": str(cluster["lemmas"][0]),
            "family_key": str(cluster["family_key"]),
            "part_of_speech": str(cluster["parts_of_speech"][0]),
            "sense_definition_en": str(cluster["sense_definition_en"]),
            "translations": [str(value) for value in cluster["translations"]],
            "contexts": contexts,
        },
        sorted(live, key=lambda item: item.created_at),
    )


def update_survivor_note(
    collection: Collection,
    note: Any,
    *,
    card: dict[str, Any],
    direction: str,
    deck_id: int,
) -> None:
    front, back = _semantic_sides(card, direction)
    note.fields[0] = front
    note.fields[1] = back
    note.tags = sorted(
        {
            *clean_managed_tags(list(note.tags)),
            f"anki_papers::{card['id']}",
            f"direction::{direction}",
            "semantic::v1",
            f"family::{safe_family_tag(str(card['family_key']))}",
        }
    )
    collection.update_note(note)
    collection.set_deck([int(item.id) for item in note.cards()], deck_id)


def merge_note_schedules(
    collection: Collection,
    survivor_note: Any,
    duplicate_notes: list[Any],
) -> dict[str, int]:
    survivor_cards = survivor_note.cards()
    if len(survivor_cards) != 1:
        raise RuntimeError("Managed survivor note must have exactly one card")
    survivor = survivor_cards[0]
    reps = int(survivor.reps)
    lapses = int(survivor.lapses)
    moved_revlog = 0
    duplicate_ids: list[int] = []
    for note in duplicate_notes:
        cards = note.cards()
        if len(cards) != 1:
            raise RuntimeError("Managed duplicate note must have exactly one card")
        card = cards[0]
        reps += int(card.reps)
        lapses += int(card.lapses)
        moved_revlog += int(
            collection.db.scalar("SELECT count(*) FROM revlog WHERE cid = ?", card.id)
            or 0
        )
        collection.db.execute(
            "UPDATE revlog SET cid = ? WHERE cid = ?",
            survivor.id,
            card.id,
        )
        duplicate_ids.append(int(note.id))
    if duplicate_ids:
        collection.remove_notes(duplicate_ids)
    survivor = collection.get_card(survivor.id)
    survivor.reps = reps
    survivor.lapses = lapses
    collection.update_card(survivor)
    return {"removed_notes": len(duplicate_ids), "moved_revlog": moved_revlog}


def migrate(
    *,
    database_path: Path,
    collection_path: Path,
    manifest_path: Path,
    source_contexts_path: Path,
    username: str,
    unknown_deck_name: str,
    report_path: Path,
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    sources = source_contexts(source_contexts_path)
    database = sqlite3.connect(database_path)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    user = database.execute(
        "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()
    if user is None:
        raise RuntimeError(f"Unknown user: {username}")
    user_id = int(user["id"])
    required_columns = {
        row[1] for row in database.execute("PRAGMA table_info(cards)")
    }
    if "semantic_version" not in required_columns:
        raise RuntimeError("Database schema does not have semantic card columns")

    highlights, chosen_by_highlight, shadowed = build_highlight_mapping(
        database,
        user_id=user_id,
        sources=sources,
    )
    collection = Collection(str(collection_path))
    account = database.execute(
        "SELECT selected_deck_id FROM anki_accounts WHERE user_id = ?", (user_id,)
    ).fetchone()
    if account is None or account["selected_deck_id"] is None:
        raise RuntimeError("The user has no selected Anki deck")
    selected_deck_id = int(account["selected_deck_id"])
    unknown_deck_id = int(collection.decks.id(unknown_deck_name))
    chosen_source_ids = set(chosen_by_highlight.values())
    shadowed_set = set(shadowed)
    known_clusters: list[tuple[dict[str, Any], dict[str, Any], list[HighlightRef]]] = []
    unknown_clusters: list[dict[str, Any]] = []
    for cluster in manifest["clusters"]:
        payload, live = semantic_card_payload(
            cluster,
            highlights=highlights,
            chosen_by_highlight=chosen_by_highlight,
            shadowed=shadowed_set,
        )
        if chosen_source_ids.intersection(cluster["source_context_ids"]):
            if not live:
                raise RuntimeError(f"Known cluster {cluster['id']} has no highlights")
            known_clusters.append((cluster, payload, live))
        else:
            unknown_clusters.append(cluster)

    used_notes: set[int] = set()
    processed_known_note_ids: set[int] = set()
    new_links: list[dict[str, Any]] = []
    removed_notes = 0
    moved_revlog = 0
    added_notes = 0
    cluster_reports: list[dict[str, Any]] = []
    for cluster, card, live in known_clusters:
        processed_known_note_ids.update(
            int(value) for value in cluster["source_note_ids"]
        )
        by_direction: dict[str, list[Any]] = defaultdict(list)
        for note_id in sorted({int(value) for value in cluster["source_note_ids"]}):
            note = collection.get_note(note_id)
            direction = note_direction(list(note.tags))
            if direction is not None:
                by_direction[direction].append(note)
        selected: dict[str, int] = {}
        for direction in ("meaning", "recall"):
            candidates = [note for note in by_direction[direction] if int(note.id) not in used_notes]
            if candidates:
                survivor = max(
                    candidates,
                    key=lambda note: schedule_rank(note.cards()[0]),
                )
                duplicates = [note for note in candidates if note.id != survivor.id]
                stats = merge_note_schedules(collection, survivor, duplicates)
                removed_notes += stats["removed_notes"]
                moved_revlog += stats["moved_revlog"]
                update_survivor_note(
                    collection,
                    survivor,
                    card=card,
                    direction=direction,
                    deck_id=selected_deck_id,
                )
            else:
                survivor = OfficialAnkiAdapter._add_note(
                    collection,
                    card,
                    direction,
                    selected_deck_id,
                )
                added_notes += 1
            used_notes.add(int(survivor.id))
            selected[direction] = int(survivor.id)
            new_links.append(
                {
                    "user_id": user_id,
                    "site_card_id": card["id"],
                    "direction": direction,
                    "note_id": int(survivor.id),
                    "note_guid": str(survivor.guid),
                }
            )
        cluster_reports.append(
            {
                "cluster_id": card["id"],
                "family_key": card["family_key"],
                "highlights": [item.id for item in live],
                "source_contexts": list(cluster["source_context_ids"]),
                "survivor_notes": selected,
            }
        )

    unknown_note_ids: set[int] = set()
    for cluster in unknown_clusters:
        unknown_note_ids.update(int(value) for value in cluster["source_note_ids"])
    unknown_card_ids: list[int] = []
    for note_id in sorted(unknown_note_ids):
        note = collection.get_note(note_id)
        note.tags = sorted({*note.tags, "anki_papers_unknown_before_migration"})
        collection.update_note(note)
        unknown_card_ids.extend(int(card.id) for card in note.cards())
    if unknown_card_ids:
        collection.set_deck(unknown_card_ids, unknown_deck_id)

    all_source_note_ids = {
        int(note_id) for source in sources for note_id in source.note_ids
    }
    accounted = processed_known_note_ids | unknown_note_ids
    leftovers = all_source_note_ids - accounted
    if leftovers:
        raise RuntimeError(f"Unaccounted source notes: {sorted(leftovers)}")

    database.execute("BEGIN IMMEDIATE")
    database.execute(
        """UPDATE sync_jobs SET state = 'cancelled', finished_at = ?, updated_at = ?
           WHERE user_id = ? AND state IN ('queued', 'running')""",
        (now(), now(), user_id),
    )
    database.execute("DELETE FROM cards WHERE user_id = ?", (user_id,))
    for cluster, card, live in known_clusters:
        representative = live[0]
        first_source_id = next(
            source_id
            for source_id in cluster["source_context_ids"]
            if source_id in chosen_source_ids
        )
        source_context = next(
            item
            for item in card["contexts"]
            if str(item["id"]) == next(
                highlight_id
                for highlight_id, source_id in chosen_by_highlight.items()
                if source_id == first_source_id
            )
        )
        database.execute(
            """INSERT INTO cards
               (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, lemma, family_key,
                part_of_speech, sense_definition_en, contexts_json, semantic_version,
                created_at, csv_exported_at, apkg_exported_at, anki_synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, 1, ?, NULL, NULL, NULL)""",
            (
                card["id"],
                user_id,
                representative.document_id,
                card["family_key"],
                normalize_target(card["family_key"]),
                representative.sentence,
                representative.page,
                json.dumps(card["translations"], ensure_ascii=False),
                str(source_context["replacement"]),
                card["lemma"],
                card["family_key"],
                card["part_of_speech"],
                card["sense_definition_en"],
                json.dumps(card["contexts"], ensure_ascii=False),
                representative.created_at,
            ),
        )
        for highlight in live:
            database.execute(
                "INSERT INTO card_highlights (card_id, highlight_id) VALUES (?, ?)",
                (card["id"], highlight.id),
            )
    for highlight_id, source_id in chosen_by_highlight.items():
        decision = next(
            item for item in manifest["decisions"] if item["source_context_id"] == source_id
        )
        analysis = decision["analysis"]
        database.execute(
            """UPDATE highlights SET translations_json = ?, replacement = ?,
               alternatives_json = '[]', status = 'ready', error = NULL, updated_at = ?
               WHERE id = ? AND user_id = ?""",
            (
                json.dumps(analysis["translations_ru"], ensure_ascii=False),
                analysis["replacement_ru"],
                now(),
                highlight_id,
                user_id,
            ),
        )
    for link in new_links:
        database.execute(
            """INSERT INTO anki_note_links
               (user_id, site_card_id, direction, note_id, note_guid, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                link["user_id"],
                link["site_card_id"],
                link["direction"],
                link["note_id"],
                link["note_guid"],
                now(),
            ),
        )
    database.commit()
    collection.close()
    database.close()

    report = {
        "username": username,
        "input_contexts": len(sources),
        "live_highlights": len(highlights),
        "shadowed_corrupt_contexts": shadowed,
        "known_clusters": len(known_clusters),
        "unknown_clusters": [
            {
                "id": cluster["id"],
                "family_key": cluster["family_key"],
                "source_context_ids": cluster["source_context_ids"],
                "source_note_ids": cluster["source_note_ids"],
            }
            for cluster in unknown_clusters
        ],
        "unknown_deck_name": unknown_deck_name,
        "unknown_notes": len(unknown_note_ids),
        "removed_duplicate_notes": removed_notes,
        "moved_revlog_rows": moved_revlog,
        "added_notes": added_notes,
        "final_managed_notes": len(new_links),
        "clusters": cluster_reports,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-contexts", type=Path, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--unknown-deck-name", default=UNKNOWN_DECK_NAME)
    args = parser.parse_args()
    report = migrate(
        database_path=args.database.resolve(),
        collection_path=args.collection.resolve(),
        manifest_path=args.manifest.resolve(),
        source_contexts_path=args.source_contexts.resolve(),
        username=args.username,
        unknown_deck_name=args.unknown_deck_name,
        report_path=args.report.resolve(),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
