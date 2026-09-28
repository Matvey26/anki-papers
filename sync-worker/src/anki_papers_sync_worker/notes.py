"""Reconcile, tag, and update managed notes in an open collection."""
from __future__ import annotations

import hashlib
import re
from typing import Any

from .errors import PermanentSyncError
from .rendering import _card_sides, _normalized, _plain

MANAGED_NOTETYPE_NAME = "Anki Papers"
SEMANTIC_NOTETYPE_NAME = "Anki Papers Semantic"


class NoteReconciler:
    def _reconcile_and_add(
        self,
        collection: Any,
        cards: list[dict[str, Any]],
        deck_id: int,
        known_links: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], int, int, int]:
        links, existing, missing = self._reconcile(
            collection, cards, add=True, deck_id=deck_id, known_links=known_links
        )
        return links, existing, missing, missing

    def _reconcile(
        self,
        collection: Any,
        cards: list[dict[str, Any]],
        *,
        add: bool,
        deck_id: int | None,
        known_links: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        links: list[dict[str, Any]] = []
        used_note_ids: set[int] = set()
        linked_note_ids = {
            (link["site_card_id"], link["direction"]): int(link["note_id"])
            for link in (known_links or [])
        }
        existing = 0
        missing = 0
        for card in cards:
            for direction in ("meaning", "recall"):
                note = None
                linked_id = linked_note_ids.get((card["id"], direction))
                if linked_id is not None and linked_id not in used_note_ids:
                    from anki.errors import NotFoundError

                    try:
                        note = collection.get_note(linked_id)
                    except NotFoundError:
                        note = None
                if note is None:
                    note = self._find_note(collection, card, direction, used_note_ids)
                if note is None:
                    missing += 1
                    if add:
                        if deck_id is None:
                            raise PermanentSyncError("deck_not_selected")
                        note = self._add_note(collection, card, direction, deck_id)
                else:
                    existing += 1
                if note is not None:
                    if add:
                        if card.get("semantic"):
                            self._update_semantic_note(collection, note, card, direction)
                        self._ensure_sync_tags(collection, note, card["id"], direction)
                    used_note_ids.add(int(note.id))
                    links.append(
                        {
                            "site_card_id": card["id"],
                            "direction": direction,
                            "note_id": int(note.id),
                            "note_guid": str(note.guid),
                        }
                    )
        return links, existing, missing

    def _find_note(
        self,
        collection: Any,
        card: dict[str, Any],
        direction: str,
        used_note_ids: set[int],
    ) -> Any | None:
        stable_tag = f"anki_papers::{card['id']}"
        searches = [
            f'tag:"{stable_tag}" tag:"direction::{direction}" -tag:"rebuild"',
            f'tag:"{stable_tag}" tag:"card::{direction}" -tag:"rebuild"',
        ]
        for query in searches:
            ids = collection.find_notes(query)
            for note_id in ids:
                if int(note_id) not in used_note_ids:
                    return collection.get_note(note_id)
        legacy_ids = collection.find_notes(
            f'(tag:"card::{direction}" OR tag:"direction::{direction}") '
            f'-tag:"rebuild"'
        )
        for note_id in legacy_ids:
            if int(note_id) in used_note_ids:
                continue
            note = collection.get_note(note_id)
            if self._legacy_matches(note, card, direction):
                return note
        return None

    @staticmethod
    def _ensure_sync_tags(collection: Any, note: Any, card_id: str, direction: str) -> None:
        """Make a reconciled legacy note identifiable without changing its content or schedule."""
        required = (f"anki_papers::{card_id}", f"direction::{direction}")
        current = list(note.tags)
        if all(tag in current for tag in required):
            return
        note.tags = [*current, *(tag for tag in required if tag not in current)]
        collection.update_note(note)

    @staticmethod
    def _legacy_matches(note: Any, card: dict[str, Any], direction: str) -> bool:
        fields = list(note.fields)
        if len(fields) < 2:
            return False
        front = _plain(fields[0])
        back = _plain(fields[1])
        target = _normalized(card["target"])
        sentence = _normalized(card["sentence"])
        if direction == "meaning":
            return front == sentence and target in front
        expected = _normalized(
            re.sub(
                re.escape(card["target"]),
                card["replacement"],
                card["sentence"],
                count=1,
                flags=re.IGNORECASE,
            )
        )
        return back == target and front.startswith(expected)

    @staticmethod
    def _add_note(collection: Any, card: dict[str, Any], direction: str, deck_id: int) -> Any:
        from anki.decks import DeckId
        from anki.utils import base91

        notetype = (
            NoteReconciler._semantic_notetype(collection)
            if card.get("semantic")
            else NoteReconciler._managed_notetype(collection)
        )
        note = collection.new_note(notetype)
        note.guid = base91(
            int.from_bytes(
                hashlib.sha256(f"{card['id']}:{direction}".encode()).digest()[:8],
                "big",
            )
        )
        front, back = _card_sides(card, direction)
        note.fields[0] = front
        note.fields[1] = back
        note.tags = [f"anki_papers::{card['id']}", f"direction::{direction}"]
        collection.add_note(note, DeckId(deck_id))
        return note

    @staticmethod
    def _update_semantic_note(collection: Any, note: Any, card: dict[str, Any], direction: str) -> None:
        front, back = _card_sides(card, direction)
        if list(note.fields[:2]) == [front, back]:
            return
        note.fields[0] = front
        note.fields[1] = back
        collection.update_note(note)

    @staticmethod
    def _managed_notetype(collection: Any) -> Any:
        notetype = collection.models.by_name(MANAGED_NOTETYPE_NAME)
        if notetype is not None:
            field_names = [field["name"] for field in notetype["flds"]]
            if field_names[:2] != ["Front", "Back"] or not notetype["tmpls"]:
                raise PermanentSyncError("managed_notetype_invalid")
            return notetype

        notetype = collection.models.new(MANAGED_NOTETYPE_NAME)
        collection.models.add_field(notetype, collection.models.new_field("Front"))
        collection.models.add_field(notetype, collection.models.new_field("Back"))
        template = collection.models.new_template("Card 1")
        template["qfmt"] = "{{Front}}"
        template["afmt"] = '{{FrontSide}}<hr id="answer">{{Back}}'
        collection.models.add_template(notetype, template)
        collection.models.add(notetype)
        return notetype

    @staticmethod
    def _semantic_notetype(collection: Any) -> Any:
        notetype = collection.models.by_name(SEMANTIC_NOTETYPE_NAME)
        if notetype is not None:
            names = [field["name"] for field in notetype["flds"]]
            if names[:2] != ["Front", "Back"] or not notetype["tmpls"]:
                raise PermanentSyncError("semantic_notetype_invalid")
            return notetype
        notetype = collection.models.new(SEMANTIC_NOTETYPE_NAME)
        collection.models.add_field(notetype, collection.models.new_field("Front"))
        collection.models.add_field(notetype, collection.models.new_field("Back"))
        template = collection.models.new_template("Card 1")
        template["qfmt"] = "{{Front}}"
        template["afmt"] = '{{FrontSide}}<hr id="answer">{{Back}}'
        collection.models.add_template(notetype, template)
        collection.models.add(notetype)
        return notetype
