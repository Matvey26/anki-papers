"""documents / highlights."""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from articles_to_anki.cards.text import normalize_target, now


def clean_highlight_rects(value: Any) -> list[dict[str, float]]:
    if not isinstance(value, list) or not value or len(value) > 16:
        raise ValueError("Expected 1-16 rectangles")
    cleaned: list[dict[str, float]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Rectangle must be an object")
        rectangle = {name: round(float(item[name]), 3) for name in ("x1", "y1", "x2", "y2")}
        if not all(math.isfinite(number) and abs(number) <= 100_000 for number in rectangle.values()):
            raise ValueError("Rectangle coordinate is out of range")
        if rectangle["x2"] <= rectangle["x1"] or rectangle["y2"] <= rectangle["y1"]:
            raise ValueError("Rectangle is empty")
        cleaned.append(rectangle)
    return cleaned


def highlight_rects_match(
    first: list[dict[str, float]],
    second: list[dict[str, float]],
    *,
    minimum_overlap: float = 0.60,
) -> bool:
    first_area = sum(
        (rectangle["x2"] - rectangle["x1"])
        * (rectangle["y2"] - rectangle["y1"])
        for rectangle in first
    )
    second_area = sum(
        (rectangle["x2"] - rectangle["x1"])
        * (rectangle["y2"] - rectangle["y1"])
        for rectangle in second
    )
    smaller_area = min(first_area, second_area)
    if smaller_area <= 0:
        return False
    intersection = 0.0
    for left in first:
        for right in second:
            width = max(0.0, min(left["x2"], right["x2"]) - max(left["x1"], right["x1"]))
            height = max(0.0, min(left["y2"], right["y2"]) - max(left["y1"], right["y1"]))
            intersection += width * height
    return intersection / smaller_area >= minimum_overlap


def highlight_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "target": row["target"],
        "sentence": row["sentence"],
        "page": row["page"],
        "rects": json.loads(row["rects_json"]),
        "translations": json.loads(row["translations_json"]),
        "replacement": row["replacement"],
        "alternatives": json.loads(row["alternatives_json"]),
        "status": row["status"],
        "error": row["error"],
        "source": row["source"],
    }


def delete_highlight_rows(
    database: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    user_id: int,
) -> None:
    if not rows:
        return
    card_ids: set[str] = set()
    removed_context_ids: dict[str, set[str]] = {}
    timestamp = now()
    for row in rows:
        linked = database.execute(
            "SELECT card_id FROM card_highlights WHERE highlight_id = ?",
            (row["id"],),
        ).fetchall()
        card_ids.update(link["card_id"] for link in linked)
        for link in linked:
            removed_context_ids.setdefault(str(link["card_id"]), set()).add(str(row["id"]))
        database.execute(
            """INSERT OR IGNORE INTO deleted_highlights
               (highlight_id, user_id, document_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (row["id"], user_id, row["document_id"], timestamp),
        )
        database.execute(
            "DELETE FROM highlights WHERE id = ? AND user_id = ?",
            (row["id"], user_id),
        )

    for card_id in card_ids:
        card = database.execute(
            "SELECT * FROM cards WHERE id = ? AND user_id = ?", (card_id, user_id)
        ).fetchone()
        if card is None:
            continue
        replacement = database.execute(
            """SELECT highlights.* FROM highlights
               JOIN card_highlights ON card_highlights.highlight_id = highlights.id
               WHERE card_highlights.card_id = ? AND highlights.user_id = ?
               ORDER BY highlights.created_at LIMIT 1""",
            (card_id, user_id),
        ).fetchone()
        if replacement is None:
            database.execute(
                "DELETE FROM cards WHERE id = ? AND user_id = ?",
                (card_id, user_id),
            )
        elif card["semantic_version"] == 1:
            removed = removed_context_ids.get(card_id, set())
            contexts = json.loads(card["contexts_json"] or "[]")
            contexts = [
                item for item in contexts
                if str(item.get("id")) not in removed
            ]
            database.execute(
                """UPDATE cards SET document_id = ?, target = ?, target_normalized = ?,
                   sentence = ?, page = ?, contexts_json = ?, csv_exported_at = NULL,
                   apkg_exported_at = NULL, anki_synced_at = NULL
                   WHERE id = ? AND user_id = ?""",
                (
                    replacement["document_id"], card["target"], card["target_normalized"],
                    replacement["sentence"], replacement["page"],
                    json.dumps(contexts, ensure_ascii=False), card_id, user_id,
                ),
            )
        else:
            database.execute(
                """UPDATE cards
                   SET document_id = ?, target = ?, target_normalized = ?,
                       sentence = ?, page = ?
                   WHERE id = ? AND user_id = ?""",
                (
                    replacement["document_id"],
                    replacement["target"],
                    normalize_target(replacement["target"]),
                    replacement["sentence"],
                    replacement["page"],
                    card_id,
                    user_id,
                ),
            )
    database.commit()
