from __future__ import annotations

import io
import sqlite3
import uuid
from pathlib import Path

from pypdf import PdfReader
from support_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.apkg as apkg_module


def test_highlight_is_enriched_saved_and_downloaded_in_pdf(
    tmp_path: Path, monkeypatch
) -> None:
    install_fake_enrichment(monkeypatch)
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client)
    client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents").fetchone()[0]

    reader_page = client.get(f"/article/{document_id}")
    response = client.post(
        f"/api/article/{document_id}/highlights",
        json={
            "id": str(uuid.uuid4()),
            "target": "robust",
            "sentence": "This is a robust result.",
            "page": 1,
            "rects": [{"x1": 80, "y1": 190, "x2": 120, "y2": 205}],
        },
        headers={"X-CSRF-Token": csrf(reader_page)},
    )
    assert response.status_code == 200
    assert response.json["highlight"]["status"] == "ready"
    assert response.json["highlight"]["translations"] == ["надёжный", "устойчивый"]

    duplicate = client.post(
        f"/api/article/{document_id}/highlights",
        json={
            "id": str(uuid.uuid4()),
            "target": "robust",
            "sentence": "This is a robust result.",
            "page": 1,
            "rects": [{"x1": 80, "y1": 190, "x2": 120, "y2": 205}],
        },
        headers={"X-CSRF-Token": csrf(reader_page)},
    )
    assert duplicate.status_code == 200
    assert duplicate.json["highlight"]["id"] == response.json["highlight"]["id"]

    stored = client.get(f"/api/article/{document_id}/highlights").json["highlights"]
    assert len(stored) == 1
    assert stored[0]["rects"] == [{"x1": 80.0, "y1": 190.0, "x2": 120.0, "y2": 205.0}]
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 1

    dashboard = client.get("/dashboard")
    assert "1 слово" in dashboard.text
    assert 'aria-label="Скачать PDF с хайлайтами"' in dashboard.text
    assert ">Открыть<" not in dashboard.text
    assert ">Отметить<" not in dashboard.text
    assert ">Прочитано<" not in dashboard.text
    download = client.get(f"/article/{document_id}/highlighted.pdf")
    assert download.status_code == 200
    assert download.headers["Content-Disposition"].startswith("attachment;")
    highlighted = PdfReader(io.BytesIO(download.data))
    annotation = highlighted.pages[0]["/Annots"][0].get_object()
    assert annotation["/Subtype"] == "/Highlight"
    assert "robust: надёжный, устойчивый" == annotation["/Contents"]


def test_apkg_export_becomes_repeatable_server_baseline(
    tmp_path: Path, monkeypatch
) -> None:
    install_fake_enrichment(monkeypatch)
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client)
    client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute(
            "SELECT id FROM documents WHERE kind = 'pdf'"
        ).fetchone()[0]
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
    client.post(
        "/upload/apkg",
        data={
            "csrf_token": csrf(dashboard),
            "file": (io.BytesIO(b"PK\x03\x04baseline"), "deck.apkg"),
        },
        content_type="multipart/form-data",
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        deck_id, stored_path = database.execute(
            "SELECT id, stored_path FROM documents WHERE kind = 'apkg'"
        ).fetchone()

    def fake_merge(source, destination, _csv_paths, _combined_csv):
        destination.write_bytes(Path(source).read_bytes() + b"-updated")

    monkeypatch.setattr(apkg_module, "merge", fake_merge)
    dashboard = client.get("/dashboard")
    first = client.post(
        "/export/apkg",
        data={"csrf_token": csrf(dashboard), "deck_id": deck_id},
    )
    assert first.status_code == 200
    assert first.data == b"PK\x03\x04baseline-updated"
    assert Path(stored_path).read_bytes() == first.data
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute(
            "SELECT COUNT(*) FROM cards WHERE apkg_exported_at IS NULL"
        ).fetchone()[0] == 0

    dashboard = client.get("/dashboard")
    assert 'class="primary" disabled' not in dashboard.text
    second = client.post(
        "/export/apkg",
        data={"csrf_token": csrf(dashboard), "deck_id": deck_id},
    )
    assert second.status_code == 200
    assert second.data == first.data
