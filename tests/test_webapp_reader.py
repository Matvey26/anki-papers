from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight, Text
from pypdf.generic import ArrayObject, DecodedStreamObject, FloatObject
from support_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.documents.pdf as ref_documents_pdf


def test_reader_saves_progress_restores_page_and_moves_article_to_top(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client)
    for filename in ("first.pdf", "second.pdf"):
        uploaded = client.post(
            "/upload/pdf",
            data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(2), filename)},
            content_type="multipart/form-data",
        )
        dashboard = client.get("/dashboard")
        assert uploaded.status_code == 302
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        rows = database.execute(
            "SELECT id, name FROM documents WHERE kind = 'pdf' ORDER BY name"
        ).fetchall()
    first_id, second_id = (rows[0][0], rows[1][0])

    first_reader = client.get(f"/article/{first_id}")
    progress = client.post(
        f"/api/article/{first_id}/progress",
        json={"page": 2},
        headers={"X-CSRF-Token": csrf(first_reader)},
    )
    assert progress.json == {"page": 2}
    restored_reader = client.get(f"/article/{first_id}")
    assert 'data-initial-page="2"' in restored_reader.text
    assert 'id="reader-completion"' in restored_reader.text
    assert 'role="switch"' in restored_reader.text

    second_reader = client.get(f"/article/{second_id}")
    dashboard = client.get("/dashboard")
    assert dashboard.text.index("second.pdf") < dashboard.text.index("first.pdf")
    assert "Стр. 2 из 2 · 100%" in dashboard.text

    marked_read = client.post(
        f"/article/{first_id}/read",
        json={"read": "1"},
        headers={"X-CSRF-Token": csrf(second_reader)},
    )
    assert marked_read.json == {"read": True}


def test_clean_pdf_removes_only_native_highlight_annotations(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    destination = tmp_path / "working.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=400)
    content = DecodedStreamObject()
    content.set_data(
        b"q\n/SPenSDK_PAGE_LIST BMC\n0 0 10 10 re f\nEMC\nQ\n"
        b"q\n0 0 m\n10 10 l\nS\nQ\n"
    )
    page.replace_contents(content)
    writer.add_annotation(
        page_number=0,
        annotation=Highlight(
            rect=(70, 180, 130, 210),
            quad_points=ArrayObject(
                FloatObject(value)
                for value in (70, 210, 130, 210, 70, 180, 130, 180)
            ),
        ),
    )
    writer.add_annotation(
        page_number=0,
        annotation=Text(rect=(10, 10, 30, 30), text="keep me"),
    )
    with source.open("wb") as stream:
        writer.write(stream)

    ref_documents_pdf.write_pdf_without_native_highlights(source, destination)

    annotations = [
        reference.get_object()["/Subtype"]
        for reference in PdfReader(destination).pages[0]["/Annots"]
    ]
    assert annotations == ["/Text"]
    cleaned_content = PdfReader(destination).pages[0].get_contents().get_data()
    assert b"SPenSDK_PAGE_LIST" not in cleaned_content
    assert b"10 10 l" in cleaned_content


def test_article_delete_removes_database_rows_and_files(
    tmp_path: Path, monkeypatch
) -> None:
    install_fake_enrichment(monkeypatch)
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client)
    client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "delete-me.pdf")},
        content_type="multipart/form-data",
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id, stored_path, source_path = database.execute(
            "SELECT id, stored_path, source_path FROM documents"
        ).fetchone()
    reader = client.get(f"/article/{document_id}")
    client.post(
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

    dashboard = client.get("/dashboard")
    assert 'id="delete-article-dialog"' in dashboard.text
    assert 'class="round-icon-button danger delete-article-trigger"' in dashboard.text
    assert "Удалить через 3" in dashboard.text
    deleted = client.post(
        f"/article/{document_id}/delete",
        data={"csrf_token": csrf(dashboard)},
        follow_redirects=True,
    )
    assert "delete-me.pdf" not in deleted.text
    assert not Path(stored_path).exists()
    assert not Path(source_path).exists()
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
