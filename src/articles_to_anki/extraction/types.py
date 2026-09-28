"""extraction / types."""
from __future__ import annotations

from dataclasses import dataclass, field

from articles_to_anki.extraction.sentences import (
    _join_raw_tokens,
    _split_outer_punctuation,
)
from articles_to_anki.models import TargetContext


@dataclass(slots=True)
class ExtractionConfig:
    render_dpi: int = 216
    min_coverage: float = 0.60
    max_vertical_spill: float = 0.30
    min_partial_coverage: float = 0.32
    max_partial_vertical_spill: float = 0.22
    min_brush_token_height_pt: float = 8.0
    top_margin_pt: float = 40.0
    bottom_margin_pt: float = 65.0
    x_tolerance: float = 1.0
    y_tolerance: float = 3.0
    max_phrase_gap_pt: float = 8.0
    min_annotation_overlap: float = 0.45


@dataclass(slots=True)
class Token:
    text: str
    page_index: int
    x0: float
    x1: float
    top: float
    bottom: float
    local_index: int
    global_index: int = -1
    break_before: bool = False
    coverage: float = 0.0
    vertical_spill: float = 0.0
    selected: bool = False

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass(slots=True)
class TargetGroup:
    page_index: int
    tokens: list[Token] = field(default_factory=list)

    @property
    def start(self) -> int:
        return self.tokens[0].global_index

    @property
    def end(self) -> int:
        return self.tokens[-1].global_index

    @property
    def coverage(self) -> float:
        return sum(token.coverage for token in self.tokens) / len(self.tokens)

    @property
    def target(self) -> str:
        joined = _join_raw_tokens(token.text for token in self.tokens)
        _, core, _ = _split_outer_punctuation(joined)
        return core


@dataclass(slots=True)
class DocumentText:
    text: str
    page_ranges: list[tuple[int, int]]
    hard_page_starts: list[bool]


@dataclass(slots=True)
class DocumentWordIndex:
    """Letter-run tokens with cached Porter stems for inexact word scans."""

    words: list[str]
    starts: list[int]
    ends: list[int]
    by_stem: dict[str, list[int]]
    by_prefix4: dict[str, list[int]]


@dataclass(slots=True)
class ExtractedHighlight:
    context: TargetContext
    rects: list[dict[str, float]]
