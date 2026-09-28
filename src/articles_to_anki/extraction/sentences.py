"""extraction / sentences."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import DocumentText, Token

import html
import re
from collections.abc import Iterable

from articles_to_anki.extraction.constants import (
    _ABBREVIATIONS,
    _LEADING_PUNCTUATION,
    _MAX_CONTEXT_CHARS,
    _TRAILING_PUNCTUATION,
    RECALL_PLACEHOLDER,
)


def _trim_context_window(
    sentence: str,
    target_start: int,
    target_end: int,
    *,
    maximum: int = _MAX_CONTEXT_CHARS,
) -> tuple[str, int, int]:
    if len(sentence) <= maximum:
        return sentence, target_start, target_end
    target_width = target_end - target_start
    available = max(40, maximum - target_width - 4)
    left_budget = int(available * 0.45)
    window_start = max(0, target_start - left_budget)
    window_end = min(len(sentence), window_start + maximum)
    if window_end == len(sentence):
        window_start = max(0, window_end - maximum)
    if window_start:
        next_space = sentence.find(" ", window_start)
        if 0 <= next_space < target_start:
            window_start = next_space + 1
    if window_end < len(sentence):
        previous_space = sentence.rfind(" ", target_end, window_end)
        if previous_space > target_end:
            window_end = previous_space
    prefix = "… " if window_start else ""
    suffix = " …" if window_end < len(sentence) else ""
    clipped = f"{prefix}{sentence[window_start:window_end].strip()}{suffix}"
    adjusted_start = len(prefix) + target_start - window_start
    return clipped, adjusted_start, adjusted_start + target_width


def _looks_like_section_heading(line: str) -> bool:
    if len(line) > 120:
        return False
    return bool(
        re.fullmatch(
            r"(?:\d+(?:\.\d+)*\.?|[A-Z]\.?)\s+"
            r"[A-Z][A-Za-z0-9 :,\-/()]+",
            line,
        )
    )


def _context_from_document_text(
    document: DocumentText,
    target: str,
    page_index: int,
    occurrence: int,
) -> tuple[str, str, str] | None:

    if page_index >= len(document.page_ranges):
        return None
    page_start, page_end = document.page_ranges[page_index]
    target_parts = [re.escape(part) for part in target.split()]
    if not target_parts:
        return None
    pattern = re.compile(
        r"(?<![A-Za-z])" + r"\s+".join(target_parts) + r"(?![A-Za-z])",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(document.text, page_start, page_end))
    if not matches:
        return None
    spans = list(_sentence_spans(document.text))
    candidates = []
    for match in matches:
        span = next(
            (
                (start, end)
                for start, end in spans
                if start <= match.start() < end
            ),
            None,
        )
        if span is None:
            continue
        candidate_sentence = document.text[span[0] : span[1]]
        if re.search(r"Figure\s+\d+\s*[|:]", candidate_sentence):
            continue
        candidates.append((match, span))
    if not candidates:
        return None
    match, span = candidates[min(occurrence, len(candidates) - 1)]
    sentence_start, sentence_end = span
    if document.hard_page_starts[page_index] and sentence_start < page_start:
        sentence_start = page_start
    sentence = document.text[sentence_start:sentence_end].strip()
    dangling_abbreviation = re.match(r"^[a-z]\.,\s+", sentence)
    if dangling_abbreviation:
        sentence_start += dangling_abbreviation.end()
        sentence = document.text[sentence_start:sentence_end].strip()
    relative_start = match.start() - sentence_start
    relative_end = match.end() - sentence_start
    sentence, relative_start, relative_end = _trim_context_window(
        sentence,
        relative_start,
        relative_end,
    )
    before = sentence[:relative_start]
    matched = sentence[relative_start:relative_end]
    after = sentence[relative_end:]
    sentence_html = (
        f"{html.escape(before)}<b>{html.escape(matched)}</b>{html.escape(after)}"
    )
    recall_template = (
        f"{html.escape(before)}{RECALL_PLACEHOLDER}{html.escape(after)}"
    )
    return sentence, sentence_html, recall_template


def _sentence_spans(text: str) -> Iterable[tuple[int, int]]:
    start = 0
    for match in re.finditer(r"[.!?:]+(?:[\"'”’)\]}]+)?", text):
        punctuation_start = match.start()
        punctuation = text[punctuation_start]
        if punctuation == ":":
            following = text[match.end() :]
            if not re.match(r"\s*[\w-]{1,30}\s*=", following):
                continue
        if (
            punctuation_start > 0
            and punctuation_start + 1 < len(text)
            and text[punctuation_start + 1].isdigit()
            and text[punctuation_start - 1].isalnum()
        ):
            continue
        token_start = punctuation_start
        while token_start > start and not text[token_start - 1].isspace():
            token_start -= 1
        token = text[token_start : punctuation_start + 1].casefold()
        if token in _ABBREVIATIONS:
            continue
        end = match.end()
        trimmed_start = start
        while trimmed_start < end and text[trimmed_start].isspace():
            trimmed_start += 1
        if trimmed_start < end:
            yield trimmed_start, end
        start = end
    while start < len(text) and text[start].isspace():
        start += 1
    if start < len(text):
        yield start, len(text)


def _sentence_bounds(
    tokens: list[Token], target_start: int, target_end: int
) -> tuple[int, int]:

    start = target_start
    while start > 0:
        if tokens[start].break_before:
            break
        if is_sentence_end(tokens[start - 1].text):
            break
        start -= 1

    end = target_end
    while end + 1 < len(tokens):
        if is_sentence_end(tokens[end].text):
            break
        if tokens[end + 1].break_before:
            break
        end += 1
    return start, end


def is_sentence_end(text: str) -> bool:
    candidate = text.strip().rstrip("\"'”’)]}")
    if not candidate or candidate.lower() in _ABBREVIATIONS:
        return False
    if re.fullmatch(r"\d+\.\d+", candidate):
        return False
    return candidate.endswith((".", "!", "?"))


def render_sentence(
    tokens: list[Token],
    target_range: tuple[int, int] | None = None,
    replacement: str | None = None,
) -> str:

    rendered: list[str] = []
    index = 0
    while index < len(tokens):
        if target_range is not None and index == target_range[0]:
            group_end = target_range[1]
            raw_target = _join_raw_tokens(
                token.text for token in tokens[index : group_end + 1]
            )
            prefix, core, suffix = _split_outer_punctuation(raw_target)
            if replacement is None:
                content = f"<b>{html.escape(core)}</b>"
            elif replacement == RECALL_PLACEHOLDER:
                content = RECALL_PLACEHOLDER
            else:
                content = html.escape(replacement)
            rendered.append(
                f"{html.escape(prefix)}{content}{html.escape(suffix)}"
            )
            index = group_end + 1
            continue
        rendered.append(html.escape(tokens[index].text))
        index += 1

    text = _join_rendered_tokens(rendered)
    return _cleanup_spacing(text)


def _join_raw_tokens(parts: Iterable[str]) -> str:
    result = ""
    previous = ""
    for part in parts:
        if not result:
            result = part
        elif previous.endswith("-") and part[:1].islower():
            result = result[:-1] + part
        else:
            result += " " + part
        previous = part
    return result


def _join_rendered_tokens(parts: Iterable[str]) -> str:
    result = ""
    previous = ""
    for part in parts:
        if not result:
            result = part
        elif previous.endswith("-") and re.match(r"(?:<[^>]+>)*[a-z]", part):
            result = result[:-1] + part
        else:
            result += " " + part
        previous = part
    return result


def _split_outer_punctuation(text: str) -> tuple[str, str, str]:
    start = 0
    while start < len(text) and text[start] in _LEADING_PUNCTUATION:
        start += 1
    end = len(text)
    while end > start and text[end - 1] in _TRAILING_PUNCTUATION:
        end -= 1
    return text[:start], text[start:end], text[end:]


def _cleanup_spacing(text: str) -> str:
    text = re.sub(r"\s+([,;:.!?%\)\]\}])", r"\1", text)
    text = re.sub(r"([\(\[\{])\s+", r"\1", text)
    text = re.sub(r"\s+([’'])s\b", r"\1s", text)
    return re.sub(r"\s{2,}", " ", text).strip()
