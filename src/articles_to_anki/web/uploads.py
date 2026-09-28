"""web / uploads."""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

from flask import abort, current_app, session
from pypdf import PdfReader

from articles_to_anki.cards.text import now
from articles_to_anki.documents.pdf import write_pdf_without_native_highlights
from articles_to_anki.storage.database import get_database


def save_document(upload: Any, kind: str) -> str:
    if upload is None or not getattr(upload, "filename", ""):
        raise ValueError("Выберите файл.")
    extension = ".pdf" if kind == "pdf" else ".apkg"
    if not upload.filename.lower().endswith(extension):
        raise ValueError(f"Нужен файл {extension.upper()}.")
    head = upload.stream.read(5)
    upload.stream.seek(0)
    if kind == "pdf" and head != b"%PDF-":
        raise ValueError("Файл не похож на PDF.")
    if kind == "apkg" and not head.startswith(b"PK"):
        raise ValueError("Файл не похож на APKG.")
    document_id = str(uuid.uuid4())
    user_dir = Path(current_app.config["DATA_DIR"]) / "uploads" / str(session["user_id"])
    user_dir.mkdir(parents=True, exist_ok=True)
    stored_path = user_dir / f"{document_id}{extension}"
    source_path = user_dir / f"{document_id}.source.pdf" if kind == "pdf" else None
    upload_path = source_path or stored_path
    upload.save(upload_path)
    size_limit = 50 * 1024 * 1024 if kind == "pdf" else 80 * 1024 * 1024
    if upload_path.stat().st_size > size_limit:
        upload_path.unlink(missing_ok=True)
        raise ValueError(f"Файл больше {size_limit // 1024 // 1024} МБ.")
    page_count = 0
    text_path: Path | None = None
    try:
        if kind == "pdf":
            reader = PdfReader(upload_path)
            page_count = len(reader.pages)
            write_pdf_without_native_highlights(upload_path, stored_path)
        get_database().execute(
            """INSERT INTO documents
               (id, user_id, kind, name, stored_path, source_path, text_path,
                page_count, size, highlight_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                document_id,
                session["user_id"],
                kind,
                Path(upload.filename).name,
                str(stored_path),
                str(source_path) if source_path else None,
                str(text_path) if text_path else None,
                page_count,
                upload_path.stat().st_size,
                "queued" if kind == "pdf" and current_app.config["AUTO_PROCESS_UPLOADS"] else "idle",
                now(),
            ),
        )
        get_database().commit()
    except Exception:
        stored_path.unlink(missing_ok=True)
        if text_path:
            text_path.unlink(missing_ok=True)
        if source_path:
            source_path.unlink(missing_ok=True)
        raise
    return document_id


def owned_document(document_id: str, kind: str) -> sqlite3.Row:
    row = get_database().execute(
        "SELECT * FROM documents WHERE id = ? AND user_id = ? AND kind = ?",
        (document_id, session["user_id"], kind),
    ).fetchone()
    if row is None:
        abort(404)
    return row
