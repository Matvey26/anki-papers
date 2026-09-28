"""rebuilding / identity."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from datetime import datetime

from articles_to_anki.rebuilding.constants import REBUILD_DECK_NAME, REBUILD_REVISION


def _cluster_id(leader: str, sentence: str) -> str:
    digest = hashlib.sha256(
        f"{REBUILD_REVISION}\0{leader}\0{sentence}".encode()
    ).digest()
    return f"rebuild-{digest[:12].hex()}"


def _rebuild_guid(front: str, deck_name: str) -> str:
    """Per-rebuild note GUID.

    Anki deduplicates imported notes by GUID, so notes whose GUIDs are derived
    only from card content would be merged into the deck where a previous
    rebuild already lives, silently emptying the fresh deck. Binding the GUID
    to the build's deck name guarantees every rebuild imports as a set of new
    notes inside its own deck, without ever touching old cards. GUIDs stay
    stable within one package, so re-importing the same file stays idempotent.
    """
    digest = hashlib.sha256(
        f"{REBUILD_REVISION}\0{deck_name}\0{front}".encode("utf-8")
    ).digest()[:9]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _stable_note_id(deck_id: int, site_id: str, direction: str) -> int:
    digest = hashlib.sha256(
        f"{REBUILD_REVISION}\0{deck_id}\0{site_id}\0{direction}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


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
