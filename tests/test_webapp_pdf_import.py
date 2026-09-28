from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from support_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.cards.llm_cache as ref_cards_llm_cache
import articles_to_anki.documents.processing as ref_documents_processing
import articles_to_anki.documents.text_cache as ref_documents_text_cache
from articles_to_anki.extract import ExtractedHighlight
from articles_to_anki.webapp import make_target_context


def test_uploaded_pdf_highlights_are_imported_automatically(
    tmp_path: Path, monkeypatch
) -> None:
    install_fake_enrichment(monkeypatch)
    extracted = ExtractedHighlight(
        context=make_target_context(
            "robust",
            "This is a robust result.",
            context_id="source-highlight",
            page=1,
        ),
        rects=[{"x1": 80, "y1": 190, "x2": 120, "y2": 205}],
    )
    monkeypatch.setattr(
        ref_documents_processing, "extract_highlights", lambda _path, **_kwargs: [extracted]
    )
    app = make_app(tmp_path, AUTO_PROCESS_UPLOADS=True)
    client = app.test_client()
    dashboard = identify(client)
    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "marked.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "Хайлайты обрабатываются" in response.text

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.row_factory = sqlite3.Row
        document = database.execute("SELECT * FROM documents").fetchone()
        highlight = database.execute("SELECT * FROM highlights").fetchone()
        assert document["highlight_status"] == "ready"
        assert document["imported_highlight_count"] == 1
        assert Path(document["source_path"]).is_file()
        assert Path(document["stored_path"]).is_file()
        assert Path(document["text_path"]).is_file()
        assert highlight["source"] == "pdf_import"
        assert highlight["status"] == "ready"
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1

    reader = client.get(f"/article/{document['id']}")
    deleted = client.delete(
        f"/api/article/{document['id']}/highlights/{highlight['id']}",
        headers={"X-CSRF-Token": csrf(reader)},
    )
    assert deleted.status_code == 200
    monkeypatch.setattr(
        ref_documents_text_cache, "extract_document_text",
        lambda _path: (_ for _ in ()).throw(AssertionError("PDF text parsed twice")),
    )
    ref_documents_processing.process_document_highlights(app, document["id"], document["user_id"])
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM deleted_highlights").fetchone()[0] == 1


def test_existing_pdf_can_be_processed_directly_without_reupload(
    tmp_path: Path, monkeypatch
) -> None:
    install_fake_enrichment(monkeypatch)
    extracted = ExtractedHighlight(
        context=make_target_context(
            "robust",
            "This is a robust result.",
            context_id="legacy-highlight",
            page=1,
        ),
        rects=[{"x1": 80, "y1": 190, "x2": 120, "y2": 205}],
    )
    monkeypatch.setattr(
        ref_documents_processing, "extract_highlights", lambda _path, **_kwargs: [extracted]
    )
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client)
    client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "legacy.pdf")},
        content_type="multipart/form-data",
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id, user_id, source_path = database.execute(
            "SELECT id, user_id, source_path FROM documents"
        ).fetchone()
        Path(source_path).unlink()
        database.execute(
            "UPDATE documents SET source_path = NULL, highlight_status = 'idle' WHERE id = ?",
            (document_id,),
        )
        database.commit()

    ref_documents_processing.process_document_highlights(app, document_id, user_id)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document = database.execute(
            "SELECT source_path, highlight_status FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        assert document[1] == "ready"
        assert Path(document[0]).is_file()
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 1

    dashboard = client.get("/dashboard")
    assert "Обработать хайлайты" not in dashboard.text
    assert "Обработать заново" not in dashboard.text


def test_pdf_import_does_not_duplicate_overlapping_reader_highlight(
    tmp_path: Path, monkeypatch
) -> None:
    install_fake_enrichment(monkeypatch)
    extracted = ExtractedHighlight(
        context=make_target_context(
            "robust",
            "This is a robust result.",
            context_id="source-highlight",
            page=1,
        ),
        rects=[{"x1": 82, "y1": 191, "x2": 119, "y2": 204}],
    )
    monkeypatch.setattr(
        ref_documents_processing, "extract_highlights", lambda _path, **_kwargs: [extracted]
    )
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client)
    client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id, user_id = database.execute(
            "SELECT id, user_id FROM documents"
        ).fetchone()
    reader = client.get(f"/article/{document_id}")
    response = client.post(
        f"/api/article/{document_id}/highlights",
        json={
            "id": str(uuid.uuid4()),
            "target": "robust",
            "sentence": "This is a robust result.",
            "page": 1,
            "rects": [{"x1": 80, "y1": 190, "x2": 120, "y2": 205}],
        },
        headers={"X-CSRF-Token": csrf(reader)},
    )
    assert response.status_code == 200

    ref_documents_processing.process_document_highlights(app, document_id, user_id)

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 1
        assert database.execute("SELECT source FROM highlights").fetchone()[0] == "reader"
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM card_highlights").fetchone()[0] == 1


def test_import_failure_is_silent_and_does_not_leave_dead_highlight(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    extracted = ExtractedHighlight(
        context=make_target_context(
            "robust",
            "This is a robust result.",
            context_id="source-highlight",
            page=1,
        ),
        rects=[{"x1": 80, "y1": 190, "x2": 120, "y2": 205}],
    )
    monkeypatch.setattr(
        ref_documents_processing, "extract_highlights", lambda _path, **_kwargs: [extracted]
    )
    monkeypatch.setattr(
        ref_cards_llm_cache, "analyse_cluster_assignment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )
    app = make_app(tmp_path, AUTO_PROCESS_UPLOADS=True)
    client = app.test_client()
    dashboard = identify(client)
    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert "Не удалось подготовить переводов" not in response.text
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document = database.execute(
            """SELECT highlight_status, highlight_error, imported_highlight_count
               FROM documents"""
        ).fetchone()
        assert document == ("ready", None, 0)
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM deleted_highlights").fetchone()[0] == 1
