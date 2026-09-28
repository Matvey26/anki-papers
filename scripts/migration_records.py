from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anki.collection import Collection
from anki_papers_sync_worker.official import _semantic_sides

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
                -(
                    min(source.note_ids)
                    if source.note_ids
                    else 2**63 - 1
                ),
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
