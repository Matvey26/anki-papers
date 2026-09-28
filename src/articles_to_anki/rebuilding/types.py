"""rebuilding / types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
