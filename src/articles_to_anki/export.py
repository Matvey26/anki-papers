from __future__ import annotations

import csv
import html
import json
import random
import re
from pathlib import Path

from .extract import RECALL_PLACEHOLDER
from .models import EnrichedItem, TargetContext


def write_extraction_json(
    path: str | Path, targets: list[TargetContext]
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {"targets": [target.model_dump() for target in targets]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return destination


def write_anki_csv(
    path: str | Path,
    targets: list[TargetContext],
    enrichments: list[EnrichedItem],
    *,
    article_tag: str,
    shuffle_seed: int | None = None,
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    by_id = {item.id: item for item in enrichments}
    if len(by_id) != len(enrichments):
        raise ValueError("Duplicate enrichment ids.")

    tag = _anki_tag(article_tag)
    card_rows: list[dict[str, str]] = []
    for target in targets:
        if target.id not in by_id:
            raise ValueError(f"No enrichment found for {target.id}.")
        enrichment = by_id[target.id]
        page_tag = f"page::{target.source_page}"
        translations = "<br>".join(
            f"• {html.escape(value)}" for value in enrichment.translations_ru
        )
        card_rows.append(
            {
                "Front": target.sentence_html,
                "Back": translations,
                "Tags": f"article::{tag} card::meaning {page_tag}",
            }
        )

        alternatives = ", ".join(
            html.escape(value)
            for value in enrichment.forbidden_alternatives_en
        )
        replacement = f"<b>{html.escape(enrichment.replacement_ru)}</b>"
        recall_front = target.recall_template_html.replace(
            RECALL_PLACEHOLDER, replacement
        )
        recall_front += (
            "<br><small>Нельзя использовать: "
            f"{alternatives}</small>"
        )
        card_rows.append(
            {
                "Front": recall_front,
                "Back": f"<b>{html.escape(target.target)}</b>",
                "Tags": f"article::{tag} card::recall {page_tag}",
            }
        )

    random.Random(shuffle_seed).shuffle(card_rows)
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["Front", "Back", "Tags"],
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(card_rows)
    return destination


def _anki_tag(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_:-]+", "_", value.strip())
    return normalized.strip("_") or "article"
