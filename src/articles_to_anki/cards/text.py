"""cards / text."""
from __future__ import annotations

import html
import re
import unicodedata
import uuid
from datetime import UTC, datetime

from articles_to_anki.constants import (
    HYPHENATED_LINE_BREAK_RE,
    MAX_TARGET_WORDS,
    WORD_RE,
)
from articles_to_anki.extraction.constants import RECALL_PLACEHOLDER
from articles_to_anki.models import TargetContext


def normalize_target(value: str) -> str:
    return normalize_selected_text(value)


def word_count_label(value: int) -> str:
    value = int(value)
    remainder = value % 100
    if 11 <= remainder <= 14:
        noun = "слов"
    elif value % 10 == 1:
        noun = "слово"
    elif 2 <= value % 10 <= 4:
        noun = "слова"
    else:
        noun = "слов"
    return f"{value} {noun}"


def normalize_selected_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value))
    value = HYPHENATED_LINE_BREAK_RE.sub("", value)
    value = value.replace("\u00ad", "")
    cleaned: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if character in {"'", "’", "‘", "ʼ", "＇"}:
            cleaned.append("'")
        elif category == "Pd" or character == "\u2212":
            cleaned.append("-")
        elif category[0] in {"L", "M", "N"}:
            cleaned.append(character.casefold())
        elif character.isspace():
            cleaned.append(" ")
        else:
            cleaned.append(" ")
    normalized = " ".join("".join(cleaned).split())
    normalized = re.sub(r"-{2,}", "-", normalized)
    normalized = re.sub(r"'{2,}", "'", normalized)
    return normalized.strip(" -'")


def is_selectable_target(value: str) -> bool:
    words = value.split()
    return (
        1 <= len(words) <= MAX_TARGET_WORDS
        and all(WORD_RE.fullmatch(word) for word in words)
        and len(value) <= 100
    )


def make_target_context(
    target: str,
    sentence: str,
    *,
    context_id: str | None = None,
    page: int = 1,
) -> TargetContext:
    match = re.search(re.escape(target), sentence, flags=re.IGNORECASE)
    if match:
        before = html.escape(sentence[: match.start()])
        selected = html.escape(sentence[match.start() : match.end()])
        after = html.escape(sentence[match.end() :])
        sentence_html = f"{before}<b>{selected}</b>{after}"
        recall_html = f"{before}{RECALL_PLACEHOLDER}{after}"
    else:
        sentence_html = html.escape(sentence)
        recall_html = sentence_html
    return TargetContext(
        id=context_id or str(uuid.uuid4()),
        target=target,
        sentence=sentence,
        sentence_html=sentence_html,
        recall_template_html=recall_html,
        source_page=page,
        highlight_coverage=1,
    )


def safe_download_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "deck"


def now() -> str:
    return datetime.now(UTC).isoformat()
