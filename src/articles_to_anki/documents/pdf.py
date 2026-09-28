"""documents / pdf."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    FloatObject,
    NameObject,
    TextStringObject,
)

from articles_to_anki.documents.highlights import clean_highlight_rects


def write_pdf_without_native_highlights(source: Path, destination: Path) -> None:
    reader = PdfReader(source)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    for page in writer.pages:
        _remove_embedded_spen_highlights(page, writer)
        annotations = page.get("/Annots")
        if annotations is not None:
            kept = ArrayObject()
            for reference in annotations:
                annotation = reference.get_object()
                if annotation.get("/Subtype") != "/Highlight":
                    kept.append(reference)
            if kept:
                page[NameObject("/Annots")] = kept
            elif NameObject("/Annots") in page:
                del page[NameObject("/Annots")]

    writer.compress_identical_objects(
        remove_duplicates=False,
        remove_unreferenced=True,
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.stem}-",
        suffix=".pdf",
        dir=destination.parent,
        delete=False,
    )
    temporary = Path(temporary_handle.name)
    try:
        with temporary_handle:
            writer.write(temporary_handle)
        PdfReader(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_embedded_spen_highlights(page: Any, writer: PdfWriter) -> None:
    contents = page.get_contents()
    if contents is None or b"/SPenSDK_PAGE_LIST" not in contents.get_data():
        return
    stream = ContentStream(contents, writer)
    kept_operations: list[tuple[list[Any], bytes]] = []
    marker_depth = 0
    removed = False
    for operands, operator in stream.operations:
        if marker_depth:
            if operator in {b"BMC", b"BDC"}:
                marker_depth += 1
            elif operator == b"EMC":
                marker_depth -= 1
            continue
        if (
            operator in {b"BMC", b"BDC"}
            and operands
            and str(operands[0]) == "/SPenSDK_PAGE_LIST"
        ):
            marker_depth = 1
            removed = True
            continue
        kept_operations.append((operands, operator))
    if not removed:
        return
    stream.operations = kept_operations
    page.replace_contents(stream)

    used_xobjects = {
        str(operands[0])
        for operands, operator in kept_operations
        if operator == b"Do" and operands
    }
    resources = page.get("/Resources")
    if resources is None:
        return
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return
    for name in list(xobjects.keys()):
        if str(name).startswith("/FXX") and str(name) not in used_xobjects:
            del xobjects[name]


def add_pdf_highlights(source: Path, rows: list[sqlite3.Row]) -> bytes:
    reader = PdfReader(source)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    for row in rows:
        page_index = int(row["page"]) - 1
        if page_index < 0 or page_index >= len(writer.pages):
            continue
        try:
            rectangles = clean_highlight_rects(json.loads(row["rects_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        quad_points = ArrayObject()
        for rectangle in rectangles:
            quad_points.extend(
                FloatObject(value)
                for value in (
                    rectangle["x1"],
                    rectangle["y2"],
                    rectangle["x2"],
                    rectangle["y2"],
                    rectangle["x1"],
                    rectangle["y1"],
                    rectangle["x2"],
                    rectangle["y1"],
                )
            )
        bounds = (
            min(rectangle["x1"] for rectangle in rectangles),
            min(rectangle["y1"] for rectangle in rectangles),
            max(rectangle["x2"] for rectangle in rectangles),
            max(rectangle["y2"] for rectangle in rectangles),
        )
        annotation = Highlight(
            rect=bounds,
            quad_points=quad_points,
            highlight_color="ffe066",
            printing=True,
        )
        translations = json.loads(row["translations_json"])
        note = row["target"]
        if translations:
            note = f"{note}: {', '.join(translations)}"
        annotation[NameObject("/Contents")] = TextStringObject(note)
        writer.add_annotation(page_number=page_index, annotation=annotation)
    stream = io.BytesIO()
    writer.write(stream)
    content = stream.getvalue()
    PdfReader(io.BytesIO(content))
    return content
