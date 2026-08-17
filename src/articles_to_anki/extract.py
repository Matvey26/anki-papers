from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageDraw
from pypdf import PdfReader

from .models import TargetContext

RECALL_PLACEHOLDER = "[[[TARGET_RU]]]"

_ABBREVIATIONS = {
    "al.",
    "approx.",
    "dr.",
    "e.",
    "e.g.",
    "eq.",
    "eqs.",
    "etc.",
    "fig.",
    "figs.",
    "g.",
    "i.e.",
    "mr.",
    "mrs.",
    "ms.",
    "no.",
    "prof.",
    "sec.",
    "secs.",
    "st.",
    "s.",
    "v.",
    "vs.",
    "v.s.",
}
_LEADING_PUNCTUATION = "\"'“‘([{"
_TRAILING_PUNCTUATION = "\"'”’)]},;:.!?"
_FOOTNOTE_URL_RE = re.compile(r"^\s*\d*\s*(?:https?://|www\.)\S+\s*$", re.IGNORECASE)
_MAX_CONTEXT_CHARS = 420


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
class ExtractedHighlight:
    context: TargetContext
    rects: list[dict[str, float]]


def yellow_mask(image: Image.Image) -> np.ndarray:
    """Return a permissive mask for translucent warm-yellow marker ink."""
    pixels = np.asarray(image.convert("RGB"))
    red = pixels[:, :, 0].astype(np.int16)
    green = pixels[:, :, 1].astype(np.int16)
    blue = pixels[:, :, 2].astype(np.int16)
    return (
        (red > 180)
        & (green > 160)
        & (blue < 210)
        & ((red - blue) > 30)
        & ((green - blue) > 20)
    )


def extract_targets(
    pdf_path: str | Path,
    *,
    config: ExtractionConfig | None = None,
    debug_dir: str | Path | None = None,
) -> list[TargetContext]:
    return [
        highlight.context
        for highlight in extract_highlights(
            pdf_path,
            config=config,
            debug_dir=debug_dir,
        )
    ]


def extract_highlights(
    pdf_path: str | Path,
    *,
    config: ExtractionConfig | None = None,
    debug_dir: str | Path | None = None,
    document_text: DocumentText | None = None,
) -> list[ExtractedHighlight]:
    config = config or ExtractionConfig()
    pdf_path = Path(pdf_path).expanduser().resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    debug_path = Path(debug_dir) if debug_dir is not None else None
    if debug_path is not None:
        debug_path.mkdir(parents=True, exist_ok=True)

    pdfium_document = pdfium.PdfDocument(str(pdf_path))
    annotation_regions = _extract_highlight_regions(pdf_path)
    page_tokens: list[list[Token]] = []
    local_groups: list[TargetGroup] = []

    try:
        with pdfplumber.open(pdf_path) as plumber_document:
            if len(plumber_document.pages) != len(pdfium_document):
                raise RuntimeError("The text and rendering backends disagree on page count.")

            for page_index, plumber_page in enumerate(plumber_document.pages):
                rendered = pdfium_document[page_index].render(
                    scale=config.render_dpi / 72
                ).to_pil().convert("RGB")
                mask = yellow_mask(rendered)
                tokens = _extract_page_tokens(plumber_page, page_index, config)
                _score_tokens(
                    tokens,
                    mask,
                    rendered,
                    plumber_page,
                    config,
                    annotation_regions[page_index],
                )
                groups = _group_selected_tokens(tokens, config)
                page_tokens.append(tokens)
                local_groups.extend(groups)

                if debug_path is not None and (
                    any(token.selected for token in tokens)
                    or any(token.coverage >= config.min_coverage for token in tokens)
                ):
                    _write_debug_image(
                        rendered,
                        tokens,
                        plumber_page,
                        debug_path / f"page-{page_index + 1:03d}.png",
                        config,
                    )
    finally:
        pdfium_document.close()

    flat_tokens: list[Token] = []
    for tokens in page_tokens:
        for token in tokens:
            token.global_index = len(flat_tokens)
            flat_tokens.append(token)

    if document_text is None:
        document_text = _extract_document_text(pdf_path)
    occurrence_counts: dict[tuple[int, str], int] = {}
    reader = PdfReader(pdf_path)
    page_boxes = [
        (
            float(page.cropbox.left),
            float(page.cropbox.bottom),
            float(page.cropbox.height),
        )
        for page in reader.pages
    ]
    highlights: list[ExtractedHighlight] = []
    for group in local_groups:
        if not group.tokens or not group.target:
            continue
        occurrence_key = (group.page_index, group.target.casefold())
        occurrence = occurrence_counts.get(occurrence_key, 0)
        context = _context_for_group(
            flat_tokens,
            group,
            pdf_path.name,
            document_text=document_text,
            occurrence=occurrence,
        )
        highlights.append(
            ExtractedHighlight(
                context=context,
                rects=_standard_rects_for_group(group, page_boxes[group.page_index]),
            )
        )
        occurrence_counts[occurrence_key] = occurrence + 1
    return highlights


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


def _score_tokens(
    tokens: list[Token],
    mask: np.ndarray,
    image: Image.Image,
    page,
    config: ExtractionConfig,
    annotation_regions: list[tuple[float, float, float, float]] | None = None,
) -> None:
    scale_x = image.width / float(page.width)
    scale_y = image.height / float(page.height)

    for token in tokens:
        x0 = max(0, int(token.x0 * scale_x))
        x1 = min(image.width, int(token.x1 * scale_x) + 1)
        y0 = max(0, int(token.top * scale_y))
        y1 = min(image.height, int(token.bottom * scale_y) + 1)
        if x1 <= x0 or y1 <= y0:
            continue

        crop = mask[y0:y1, x0:x1]
        pixel_coverage = float(crop.mean()) if crop.size else 0.0
        annotation_coverage = max(
            (
                _rectangle_overlap_ratio(
                    (token.x0, token.top, token.x1, token.bottom),
                    region,
                )
                for region in (annotation_regions or [])
            ),
            default=0.0,
        )
        token.coverage = max(pixel_coverage, annotation_coverage)
        height_px = y1 - y0
        above = mask[max(0, y0 - height_px) : y0, x0:x1]
        below = mask[y1 : min(image.height, y1 + height_px), x0:x1]
        above_ratio = float(above.mean()) if above.size else 0.0
        below_ratio = float(below.mean()) if below.size else 0.0
        token.vertical_spill = max(above_ratio, below_ratio)
        brush_selected = (
            token.height >= config.min_brush_token_height_pt
            and (
                (
                    pixel_coverage >= config.min_coverage
                    and token.vertical_spill < config.max_vertical_spill
                )
                or (
                    pixel_coverage >= config.min_partial_coverage
                    and token.vertical_spill <= config.max_partial_vertical_spill
                )
            )
        )
        token.selected = (
            (brush_selected or annotation_coverage >= config.min_annotation_overlap)
            and bool(re.search(r"[A-Za-z]", token.text))
        )


def _extract_highlight_regions(
    pdf_path: Path,
) -> list[list[tuple[float, float, float, float]]]:
    """Return native PDF highlight quads in pdfplumber's top-origin coordinates."""
    reader = PdfReader(pdf_path)
    pages: list[list[tuple[float, float, float, float]]] = []
    for page in reader.pages:
        crop_box = page.cropbox
        crop_left = float(crop_box.left)
        crop_bottom = float(crop_box.bottom)
        page_height = float(crop_box.height)
        regions: list[tuple[float, float, float, float]] = []
        for annotation_reference in page.get("/Annots") or []:
            annotation = annotation_reference.get_object()
            if str(annotation.get("/Subtype")) != "/Highlight":
                continue
            quad_points = annotation.get("/QuadPoints")
            if quad_points and len(quad_points) % 8 == 0:
                for offset in range(0, len(quad_points), 8):
                    regions.append(
                        _pdf_quad_to_region(
                            [float(value) for value in quad_points[offset : offset + 8]],
                            crop_left=crop_left,
                            crop_bottom=crop_bottom,
                            page_height=page_height,
                        )
                    )
                continue
            rectangle = annotation.get("/Rect")
            if rectangle and len(rectangle) == 4:
                x0, y0, x1, y1 = [float(value) for value in rectangle]
                regions.append(
                    (
                        min(x0, x1) - crop_left,
                        page_height - (max(y0, y1) - crop_bottom),
                        max(x0, x1) - crop_left,
                        page_height - (min(y0, y1) - crop_bottom),
                    )
                )
        pages.append(regions)
    return pages


def _pdf_quad_to_region(
    points: list[float],
    *,
    crop_left: float,
    crop_bottom: float,
    page_height: float,
) -> tuple[float, float, float, float]:
    xs = points[0::2]
    ys = points[1::2]
    return (
        min(xs) - crop_left,
        page_height - (max(ys) - crop_bottom),
        max(xs) - crop_left,
        page_height - (min(ys) - crop_bottom),
    )


def _rectangle_overlap_ratio(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    return intersection / area if area else 0.0


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


def _extract_document_text(pdf_path: Path) -> DocumentText:
    reader = PdfReader(str(pdf_path))
    pages: list[str] = []
    hard_page_starts: list[bool] = []
    for page in reader.pages:
        raw = page.extract_text() or ""
        raw_lines = raw.splitlines()
        abstract_index = next(
            (
                index
                for index, line in enumerate(raw_lines)
                if line.strip().casefold() == "abstract"
            ),
            None,
        )
        if abstract_index is not None:
            raw_lines = raw_lines[abstract_index + 1 :]
        lines: list[str] = []
        first_nonempty_seen = False
        hard_start = False
        for line in raw_lines:
            stripped = line.strip()
            if not stripped or re.fullmatch(r"\d{1,3}", stripped):
                continue
            if _FOOTNOTE_URL_RE.fullmatch(stripped):
                continue
            if not first_nonempty_seen:
                hard_start = _looks_like_section_heading(stripped)
                first_nonempty_seen = True
            if _looks_like_section_heading(stripped):
                continue
            lines.append(stripped)
        page_text = "\n".join(lines)
        page_text = re.sub(
            r"(?<=[a-z])- *\n *(?=[a-z])",
            "",
            page_text,
        )
        page_text = re.sub(r"\s*\n\s*", " ", page_text)
        page_text = re.sub(r"\s{2,}", " ", page_text).strip()
        pages.append(page_text)
        hard_page_starts.append(hard_start)

    combined = ""
    page_ranges: list[tuple[int, int]] = []
    for page_text in pages:
        if combined:
            combined += " "
        start = len(combined)
        combined += page_text
        page_ranges.append((start, len(combined)))
    return DocumentText(
        text=combined,
        page_ranges=page_ranges,
        hard_page_starts=hard_page_starts,
    )


def extract_document_text(pdf_path: str | Path) -> DocumentText:
    """Extract cleaned, page-addressable prose for article-context mining."""
    path = Path(pdf_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")
    return _extract_document_text(path)


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
        span = next(
            (
                (start, end)
                for start, end in sentence_spans
                if start <= match.start() < end
            ),
            None,
        )
        if span is None:
            continue
        sentence_start, sentence_end = span
        sentence = document.text[sentence_start:sentence_end].strip()
        relative_start = match.start() - sentence_start
        relative_end = match.end() - sentence_start
        sentence, relative_start, relative_end = _trim_context_window(
            sentence,
            relative_start,
            relative_end,
        )
        surface = sentence[relative_start:relative_end]
        key = (surface.casefold(), " ".join(sentence.casefold().split()))
        if key in seen:
            continue
        seen.add(key)
        page = _page_for_position(document, match.start())
        before = sentence[:relative_start]
        after = sentence[relative_end:]
        digest = hashlib.sha256(
            f"{page}\0{match.start()}\0{surface}\0{sentence}".encode()
        ).hexdigest()[:16]
        found.append(
            TargetContext(
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
        )
    return found


def _page_for_position(document: DocumentText, position: int) -> int:
    for page_index, (start, end) in enumerate(document.page_ranges):
        if start <= position <= end:
            return page_index + 1
    return len(document.page_ranges)


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


def _write_debug_image(
    image: Image.Image,
    tokens: list[Token],
    page,
    destination: Path,
    config: ExtractionConfig,
) -> None:
    debug = image.copy()
    draw = ImageDraw.Draw(debug)
    scale_x = image.width / float(page.width)
    scale_y = image.height / float(page.height)
    for token in tokens:
        if token.coverage < config.min_coverage:
            continue
        box = (
            int(token.x0 * scale_x),
            int(token.top * scale_y),
            int(token.x1 * scale_x),
            int(token.bottom * scale_y),
        )
        color = "#00a000" if token.selected else "#d03030"
        width = max(2, round(config.render_dpi / 90))
        draw.rectangle(box, outline=color, width=width)
        draw.text(
            (box[0], max(0, box[1] - 12)),
            f"{token.coverage:.2f}",
            fill=color,
            stroke_width=1,
            stroke_fill="white",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    debug.save(destination)
