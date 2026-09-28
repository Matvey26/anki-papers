from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from anki.collection import Collection
from anki_papers_sync_worker.official import OfficialAnkiAdapter
from migration_records import (
    UNKNOWN_DECK_NAME,
    HighlightRef,
    build_highlight_mapping,
    load_json,
    merge_note_schedules,
    note_direction,
    now,
    schedule_rank,
    semantic_card_payload,
    source_contexts,
    update_survivor_note,
)

from articles_to_anki.cards.text import normalize_target


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
