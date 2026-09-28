"""extraction / document."""
from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

from articles_to_anki.extraction.constants import _FOOTNOTE_URL_RE
from articles_to_anki.extraction.sentences import _looks_like_section_heading
from articles_to_anki.extraction.types import DocumentText


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
