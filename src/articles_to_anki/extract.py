"""extract."""
from __future__ import annotations

from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from pypdf import PdfReader

from articles_to_anki.extraction.document import _extract_document_text
from articles_to_anki.extraction.pixels import (
    _extract_highlight_regions,
    _score_tokens,
    _write_debug_image,
    yellow_mask,
)
from articles_to_anki.extraction.tokens import (
    _context_for_group,
    _extract_page_tokens,
    _group_selected_tokens,
    _standard_rects_for_group,
)
from articles_to_anki.extraction.types import (
    DocumentText,
    ExtractedHighlight,
    ExtractionConfig,
    TargetGroup,
    Token,
)
from articles_to_anki.models import TargetContext


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


# Public imports retained for existing CLI and library callers.
from articles_to_anki.extraction.constants import _ABBREVIATIONS as _ABBREVIATIONS
from articles_to_anki.extraction.constants import _FOOTNOTE_URL_RE as _FOOTNOTE_URL_RE
from articles_to_anki.extraction.constants import (
    _LEADING_PUNCTUATION as _LEADING_PUNCTUATION,
)
from articles_to_anki.extraction.constants import (
    _MAX_CONTEXT_CHARS as _MAX_CONTEXT_CHARS,
)
from articles_to_anki.extraction.constants import _PORTER as _PORTER
from articles_to_anki.extraction.constants import (
    _TRAILING_PUNCTUATION as _TRAILING_PUNCTUATION,
)
from articles_to_anki.extraction.constants import (
    _VARIANT_FUZZY_SCORE_CUTOFF as _VARIANT_FUZZY_SCORE_CUTOFF,
)
from articles_to_anki.extraction.constants import (
    _VARIANT_MAX_LENGTH_DIFF as _VARIANT_MAX_LENGTH_DIFF,
)
from articles_to_anki.extraction.constants import _VARIANT_TOKEN_RE as _VARIANT_TOKEN_RE
from articles_to_anki.extraction.constants import (
    RECALL_PLACEHOLDER as RECALL_PLACEHOLDER,
)
from articles_to_anki.extraction.document import (
    _extract_document_text as _extract_document_text,
)
from articles_to_anki.extraction.document import (
    extract_document_text as extract_document_text,
)
from articles_to_anki.extraction.pixels import (
    _extract_highlight_regions as _extract_highlight_regions,
)
from articles_to_anki.extraction.pixels import (
    _pdf_quad_to_region as _pdf_quad_to_region,
)
from articles_to_anki.extraction.pixels import (
    _rectangle_overlap_ratio as _rectangle_overlap_ratio,
)
from articles_to_anki.extraction.pixels import _score_tokens as _score_tokens
from articles_to_anki.extraction.pixels import _write_debug_image as _write_debug_image
from articles_to_anki.extraction.pixels import yellow_mask as yellow_mask
from articles_to_anki.extraction.search import _page_for_position as _page_for_position
from articles_to_anki.extraction.search import (
    _target_context_for_match as _target_context_for_match,
)
from articles_to_anki.extraction.search import (
    document_word_index as document_word_index,
)
from articles_to_anki.extraction.search import (
    find_article_contexts as find_article_contexts,
)
from articles_to_anki.extraction.search import (
    find_variant_article_contexts as find_variant_article_contexts,
)
from articles_to_anki.extraction.sentences import _cleanup_spacing as _cleanup_spacing
from articles_to_anki.extraction.sentences import (
    _context_from_document_text as _context_from_document_text,
)
from articles_to_anki.extraction.sentences import _join_raw_tokens as _join_raw_tokens
from articles_to_anki.extraction.sentences import (
    _join_rendered_tokens as _join_rendered_tokens,
)
from articles_to_anki.extraction.sentences import (
    _looks_like_section_heading as _looks_like_section_heading,
)
from articles_to_anki.extraction.sentences import _sentence_bounds as _sentence_bounds
from articles_to_anki.extraction.sentences import _sentence_spans as _sentence_spans
from articles_to_anki.extraction.sentences import (
    _split_outer_punctuation as _split_outer_punctuation,
)
from articles_to_anki.extraction.sentences import (
    _trim_context_window as _trim_context_window,
)
from articles_to_anki.extraction.sentences import is_sentence_end as is_sentence_end
from articles_to_anki.extraction.sentences import render_sentence as render_sentence
from articles_to_anki.extraction.tokens import _context_for_group as _context_for_group
from articles_to_anki.extraction.tokens import (
    _extract_page_tokens as _extract_page_tokens,
)
from articles_to_anki.extraction.tokens import (
    _group_selected_tokens as _group_selected_tokens,
)
from articles_to_anki.extraction.tokens import (
    _standard_rects_for_group as _standard_rects_for_group,
)
from articles_to_anki.extraction.types import DocumentText as DocumentText
from articles_to_anki.extraction.types import DocumentWordIndex as DocumentWordIndex
from articles_to_anki.extraction.types import ExtractedHighlight as ExtractedHighlight
from articles_to_anki.extraction.types import ExtractionConfig as ExtractionConfig
from articles_to_anki.extraction.types import TargetGroup as TargetGroup
from articles_to_anki.extraction.types import Token as Token
