"""documents / text_cache."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from articles_to_anki.constants import LOGGER
from articles_to_anki.extraction.document import extract_document_text
from articles_to_anki.extraction.types import DocumentText
from articles_to_anki.storage.files import replace_managed_file


def document_text_cache_path(document: sqlite3.Row) -> Path:
    return Path(document["stored_path"]).with_suffix(".text.json")


def write_document_text_cache(path: Path, document_text: DocumentText) -> None:
    payload = json.dumps(
        {
            "version": 1,
            "text": document_text.text,
            "page_ranges": document_text.page_ranges,
            "hard_page_starts": document_text.hard_page_starts,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_managed_file(path, payload)


def read_document_text_cache(path: Path) -> DocumentText:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Document-text cache must contain an object")
    if payload.get("version") != 1 or not isinstance(payload.get("text"), str):
        raise ValueError("Unsupported document-text cache")
    page_ranges = [tuple(item) for item in payload.get("page_ranges", [])]
    hard_page_starts = payload.get("hard_page_starts", [])
    if not all(
        len(item) == 2 and all(isinstance(value, int) for value in item)
        for item in page_ranges
    ) or not all(isinstance(value, bool) for value in hard_page_starts):
        raise ValueError("Invalid document-text cache")
    if len(page_ranges) != len(hard_page_starts):
        raise ValueError("Invalid document-text page metadata")
    return DocumentText(
        text=payload["text"],
        page_ranges=page_ranges,
        hard_page_starts=hard_page_starts,
    )


def load_or_extract_document_text(
    database: sqlite3.Connection,
    document: sqlite3.Row,
) -> DocumentText:
    cached_path = Path(document["text_path"]) if document["text_path"] else None
    if cached_path is not None and cached_path.is_file():
        try:
            return read_document_text_cache(cached_path)
        except (OSError, TypeError, ValueError):
            LOGGER.warning("Rebuilding invalid article-text cache %s", cached_path)

    pdf_path = Path(document["source_path"] or document["stored_path"])
    parsed = extract_document_text(pdf_path)
    cached_path = document_text_cache_path(document)
    write_document_text_cache(cached_path, parsed)
    database.execute(
        "UPDATE documents SET text_path = ? WHERE id = ? AND user_id = ?",
        (str(cached_path), document["id"], document["user_id"]),
    )
    return parsed
