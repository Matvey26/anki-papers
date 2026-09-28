"""rebuilding / schedules."""
from __future__ import annotations

import json
import sqlite3

from articles_to_anki.rebuilding.constants import _SCHEDULE_FIELDS


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


def _deck_names(connection: sqlite3.Connection) -> dict[int, str]:
    """Map deck id to name, covering both modern col JSON and legacy decks table."""
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
            names: dict[int, str] = {}
            for deck_id, deck in decks.items():
                name = deck.get("name") if isinstance(deck, dict) else None
                if isinstance(name, str):
                    names[int(deck_id)] = name
            if names:
                return names
    try:
        return {
            int(deck_id): str(name)
            for deck_id, name in connection.execute("SELECT id, name FROM decks")
        }
    except (sqlite3.OperationalError, TypeError, ValueError):
        return {}


def _resolve_source_decks(
    connection: sqlite3.Connection, selected_deck_id: int
) -> set[int]:
    """Expand a chosen deck to itself and all of its subdecks."""
    names = _deck_names(connection)
    selected_name = names.get(selected_deck_id)
    if selected_name is None:
        return {selected_deck_id}
    prefix = f"{selected_name}::"
    return {
        deck_id
        for deck_id, name in names.items()
        if deck_id == selected_deck_id or name.startswith(prefix)
    }


def _extract_schedules(
    connection: sqlite3.Connection,
    deck_ids: set[int] | None = None,
) -> dict[tuple[str, str], dict[str, int]]:
    """Read card scheduling keyed by site card and direction.

    When deck_ids is given, only cards inside those decks (and the decks they
    expand to) contribute; otherwise every card in the collection qualifies.
    """
    try:
        parameters: list[int] = []
        deck_filter = ""
        if deck_ids:
            placeholders = ",".join("?" for _ in deck_ids)
            deck_filter = f" AND c.did IN ({placeholders})"
            parameters.extend(sorted(deck_ids))
        rows = connection.execute(
            f"""SELECT n.tags AS tags, c.type, c.queue, c.due, c.ivl, c.factor,
               c.reps, c.lapses, c.left, c.odue, c.odid
               FROM notes n JOIN cards c ON c.nid = n.id{deck_filter}""",
            parameters,
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
