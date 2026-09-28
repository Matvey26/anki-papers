"""extraction / search."""
from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Iterable

from rapidfuzz import fuzz

from articles_to_anki.extraction.constants import (
    _PORTER,
    _VARIANT_FUZZY_SCORE_CUTOFF,
    _VARIANT_MAX_LENGTH_DIFF,
    _VARIANT_TOKEN_RE,
    RECALL_PLACEHOLDER,
)
from articles_to_anki.extraction.sentences import _sentence_spans, _trim_context_window
from articles_to_anki.extraction.types import DocumentText, DocumentWordIndex
from articles_to_anki.models import TargetContext


def find_article_contexts(
    document: DocumentText,
    targets: Iterable[str],
    *,
    limit_per_target: int = 12,
) -> list[TargetContext]:
    """Find literal target occurrences without creating PDF highlights."""
    found: list[TargetContext] = []
    seen: set[tuple[str, str]] = set()
    sentence_spans = list(_sentence_spans(document.text))
    normalized_targets = list(
        dict.fromkeys(" ".join(value.split()) for value in targets if value.strip())
    )
    raw_matches: list[tuple[int, int, str, re.Match[str]]] = []
    for target in normalized_targets:
        parts = [re.escape(part) for part in target.split()]
        pattern = re.compile(
            r"(?<![A-Za-z])" + r"\s+".join(parts) + r"(?![A-Za-z])",
            re.IGNORECASE,
        )
        for match in pattern.finditer(document.text):
            raw_matches.append((match.start(), match.end(), target, match))
    raw_matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    selected_matches: list[tuple[int, int, str, re.Match[str]]] = []
    counts: dict[str, int] = {}
    for candidate in raw_matches:
        start, end, target, _ = candidate
        if counts.get(target, 0) >= limit_per_target:
            continue
        if any(start < other_end and end > other_start for other_start, other_end, *_ in selected_matches):
            continue
        selected_matches.append(candidate)
        counts[target] = counts.get(target, 0) + 1

    for _, _, _, match in selected_matches:
        context = _target_context_for_match(
            document,
            sentence_spans,
            match.start(),
            match.end(),
        )
        if context is None:
            continue
        key = (context.target.casefold(), " ".join(context.sentence.casefold().split()))
        if key in seen:
            continue
        seen.add(key)
        found.append(context)
    return found


def document_word_index(document: DocumentText) -> DocumentWordIndex:
    """Tokenize document prose once for stem-based variant scanning."""
    words: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    by_stem: dict[str, list[int]] = {}
    by_prefix4: dict[str, list[int]] = {}
    for match in _VARIANT_TOKEN_RE.finditer(document.text):
        token = match.group(0)
        index = len(words)
        words.append(token)
        starts.append(match.start())
        ends.append(match.end())
        by_stem.setdefault(_PORTER.stem(token.casefold()), []).append(index)
        if len(token) >= 4:
            by_prefix4.setdefault(token.casefold()[:4], []).append(index)
    return DocumentWordIndex(
        words=words,
        starts=starts,
        ends=ends,
        by_stem=by_stem,
        by_prefix4=by_prefix4,
    )


def find_variant_article_contexts(
    document: DocumentText,
    targets: Iterable[str],
    *,
    index: DocumentWordIndex | None = None,
    limit_per_target: int = 8,
) -> list[TargetContext]:
    """Find inflected or derived target forms (imprecise spelling match)."""
    if index is None:
        index = document_word_index(document)
    found: list[TargetContext] = []
    seen: set[tuple[str, str]] = set()
    sentence_spans = list(_sentence_spans(document.text))
    normalized_targets = list(
        dict.fromkeys(" ".join(value.split()) for value in targets if value.strip())
    )
    for target in normalized_targets:
        words = re.findall(r"[A-Za-z]+", target.casefold())
        if not words or any(len(word) < 3 for word in words):
            continue
        target_stems = [_PORTER.stem(word) for word in words]
        taken = 0
        for first_index in index.by_stem.get(target_stems[0], []):
            if taken >= limit_per_target:
                break
            last = first_index + len(target_stems) - 1
            if last >= len(index.words):
                break
            if any(
                _PORTER.stem(index.words[position].casefold())
                != target_stems[position - first_index]
                for position in range(first_index, last + 1)
            ):
                continue
            if len(target_stems) == 1:
                surface = document.text[
                    index.starts[first_index] : index.ends[first_index]
                ]
            else:
                surface = " ".join(
                    document.text[
                        index.starts[first_index] : index.ends[last]
                    ].split()
                )
            if surface.casefold() == target.casefold():
                continue
            context = _target_context_for_match(
                document,
                sentence_spans,
                index.starts[first_index],
                index.ends[last],
            )
            if context is None:
                continue
            context.target = surface
            key = (surface.casefold(), " ".join(context.sentence.casefold().split()))
            if key in seen:
                continue
            seen.add(key)
            found.append(context)
            taken += 1

        if len(words) == 1:
            for candidate_index in index.by_prefix4.get(words[0][:4], []):
                if taken >= limit_per_target:
                    break
                candidate = index.words[candidate_index]
                if (
                    candidate.casefold() == words[0]
                    or len(candidate) < 4
                    or abs(len(candidate) - len(words[0])) > _VARIANT_MAX_LENGTH_DIFF
                    or _PORTER.stem(candidate.casefold()) in target_stems
                    or fuzz.WRatio(words[0], candidate) < _VARIANT_FUZZY_SCORE_CUTOFF
                ):
                    continue
                context = _target_context_for_match(
                    document,
                    sentence_spans,
                    index.starts[candidate_index],
                    index.ends[candidate_index],
                )
                if context is None:
                    continue
                surface = context.target
                key = (surface.casefold(), " ".join(context.sentence.casefold().split()))
                if key in seen:
                    continue
                seen.add(key)
                found.append(context)
                taken += 1
    return found


def _target_context_for_match(
    document: DocumentText,
    sentence_spans: list[tuple[int, int]],
    start: int,
    end: int,
) -> TargetContext | None:
    span = next(
        (
            (span_start, span_end)
            for span_start, span_end in sentence_spans
            if span_start <= start < span_end
        ),
        None,
    )
    if span is None:
        return None
    sentence_start, sentence_end = span
    sentence = document.text[sentence_start:sentence_end].strip()
    relative_start = start - sentence_start
    relative_end = end - sentence_start
    sentence, relative_start, relative_end = _trim_context_window(
        sentence,
        relative_start,
        relative_end,
    )
    surface = sentence[relative_start:relative_end]
    page = _page_for_position(document, start)
    before = sentence[:relative_start]
    after = sentence[relative_end:]
    digest = hashlib.sha256(
        f"{page}\0{start}\0{surface}\0{sentence}".encode()
    ).hexdigest()[:16]
    return TargetContext(
        id=digest,
        target=surface,
        sentence=sentence,
        sentence_html=(
            f"{html.escape(before)}<b>{html.escape(surface)}</b>"
            f"{html.escape(after)}"
        ),
        recall_template_html=(
            f"{html.escape(before)}{RECALL_PLACEHOLDER}{html.escape(after)}"
        ),
        source_page=page,
        highlight_coverage=0,
    )


def _page_for_position(document: DocumentText, position: int) -> int:
    for page_index, (start, end) in enumerate(document.page_ranges):
        if start <= position <= end:
            return page_index + 1
    return len(document.page_ranges)
