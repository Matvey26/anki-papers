"""extraction / tokens."""
from __future__ import annotations

import hashlib
import re

from articles_to_anki.extraction.constants import RECALL_PLACEHOLDER
from articles_to_anki.extraction.sentences import (
    _context_from_document_text,
    _sentence_bounds,
    _split_outer_punctuation,
    is_sentence_end,
    render_sentence,
)
from articles_to_anki.extraction.types import (
    DocumentText,
    ExtractionConfig,
    TargetGroup,
    Token,
)
from articles_to_anki.models import TargetContext


def _standard_rects_for_group(
    group: TargetGroup,
    page_box: tuple[float, float, float],
) -> list[dict[str, float]]:
    crop_left, crop_bottom, page_height = page_box
    lines: list[list[Token]] = []
    for token in group.tokens:
        if not lines:
            lines.append([token])
            continue
        previous = lines[-1][-1]
        same_line = abs(token.top - previous.top) <= max(
            2.0,
            0.25 * max(token.height, previous.height),
        )
        if same_line:
            lines[-1].append(token)
        else:
            lines.append([token])

    rectangles: list[dict[str, float]] = []
    for line in lines:
        left = min(token.x0 for token in line)
        right = max(token.x1 for token in line)
        top = min(token.top for token in line)
        bottom = max(token.bottom for token in line)
        rectangles.append(
            {
                "x1": round(crop_left + left, 3),
                "y1": round(crop_bottom + page_height - bottom, 3),
                "x2": round(crop_left + right, 3),
                "y2": round(crop_bottom + page_height - top, 3),
            }
        )
    return rectangles


def _extract_page_tokens(page, page_index: int, config: ExtractionConfig) -> list[Token]:
    raw_words = page.extract_words(
        use_text_flow=True,
        x_tolerance=config.x_tolerance,
        y_tolerance=config.y_tolerance,
        extra_attrs=["upright"],
    )
    filtered = [
        word
        for word in raw_words
        if word.get("upright", True)
        and word["top"] >= config.top_margin_pt
        and word["bottom"] <= page.height - config.bottom_margin_pt
        and re.search(r"\S", word["text"])
    ]

    tokens: list[Token] = []
    previous: Token | None = None
    for local_index, word in enumerate(filtered):
        token = Token(
            text=word["text"],
            page_index=page_index,
            x0=float(word["x0"]),
            x1=float(word["x1"]),
            top=float(word["top"]),
            bottom=float(word["bottom"]),
            local_index=local_index,
        )
        if previous is not None:
            vertical_jump = token.top - previous.top
            height = max(token.height, previous.height, 1.0)
            token.break_before = (
                vertical_jump > max(20.0, 1.8 * height)
                or token.top < previous.top - 2.0
            )
        tokens.append(token)
        previous = token
    return tokens


def _group_selected_tokens(
    tokens: list[Token], config: ExtractionConfig
) -> list[TargetGroup]:
    selected = [token for token in tokens if token.selected]
    groups: list[TargetGroup] = []
    for token in selected:
        if not groups:
            groups.append(TargetGroup(page_index=token.page_index, tokens=[token]))
            continue

        previous = groups[-1].tokens[-1]
        same_line = abs(token.top - previous.top) <= max(
            2.0, 0.25 * max(token.height, previous.height)
        )
        horizontal_gap = token.x0 - previous.x1
        intervening = tokens[previous.local_index + 1 : token.local_index]
        skippable_math = (
            0 < len(intervening) <= 2
            and all(not re.search(r"[A-Za-z0-9]", item.text) for item in intervening)
        )
        adjacent = (
            token.local_index == previous.local_index + 1 or skippable_math
        )
        wrapped_line = (
            adjacent
            and token.top > previous.top + 2.0
            and token.top - previous.top <= 1.6 * max(token.height, previous.height)
            and token.x0 < previous.x0
            and not is_sentence_end(previous.text)
        )
        wrapped_hyphen = (
            adjacent
            and previous.text.endswith("-")
            and token.top > previous.top + 2.0
        )
        if adjacent and (
            (same_line and horizontal_gap <= config.max_phrase_gap_pt)
            or (same_line and skippable_math and horizontal_gap <= 60.0)
            or wrapped_line
            or wrapped_hyphen
        ):
            groups[-1].tokens.append(token)
        else:
            groups.append(TargetGroup(page_index=token.page_index, tokens=[token]))

    for group in groups:
        if len(group.tokens) < 3:
            continue
        _, first_core, _ = _split_outer_punctuation(group.tokens[0].text)
        if first_core.casefold() in {"and", "but", "or"}:
            group.tokens.pop(0)
    return groups


def _context_for_group(
    tokens: list[Token],
    group: TargetGroup,
    source_name: str,
    *,
    document_text: DocumentText | None = None,
    occurrence: int = 0,
) -> TargetContext:
    text_context = (
        _context_from_document_text(
            document_text,
            group.target,
            group.page_index,
            occurrence,
        )
        if document_text is not None
        else None
    )
    if text_context is not None:
        sentence, sentence_html, recall_template = text_context
    else:
        start, end = _sentence_bounds(tokens, group.start, group.end)
        sentence_tokens = tokens[start : end + 1]
        relative_start = group.start - start
        relative_end = group.end - start
        sentence = render_sentence(sentence_tokens)
        sentence_html = render_sentence(
            sentence_tokens,
            target_range=(relative_start, relative_end),
            replacement=None,
        )
        recall_template = render_sentence(
            sentence_tokens,
            target_range=(relative_start, relative_end),
            replacement=RECALL_PLACEHOLDER,
        )
    digest = hashlib.sha256(
        (
            f"{source_name}\0{group.page_index}\0{group.start}\0"
            f"{group.target}\0{sentence}"
        ).encode()
    ).hexdigest()[:16]
    return TargetContext(
        id=digest,
        target=group.target,
        sentence=sentence,
        sentence_html=sentence_html,
        recall_template_html=recall_template,
        source_page=group.page_index + 1,
        highlight_coverage=round(group.coverage, 4),
    )
