from __future__ import annotations

import io
import json
import re
import sqlite3
from pathlib import Path

from pypdf import PdfWriter

from articles_to_anki.webapp import create_app


def csrf(response) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match
    return match.group(1).decode()


def pdf_bytes() -> io.BytesIO:
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.write(stream)
    stream.seek(0)
    return stream


def make_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATA_DIR": tmp_path,
            "DATABASE": tmp_path / "app.sqlite3",
        }
    )


def register(client, username: str = "reader"):
    token = csrf(client.get("/register"))
    return client.post(
        "/register",
        data={"csrf_token": token, "username": username, "password": "secret12"},
        follow_redirects=True,
    )


def test_register_upload_add_and_export_only_new_cards(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    response = register(client)
    assert response.status_code == 200
    assert "Библиотека" in response.text

    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "Статья загружена" in response.text

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id, text_path = database.execute(
            "SELECT id, text_path FROM documents WHERE kind = 'pdf'"
        ).fetchone()
    Path(text_path).write_text(json.dumps(["This is a robust result."]), encoding="utf-8")
    reader = client.get(f"/article/{document_id}")
    assert 'class="word ' in reader.text
    assert 'data-word="robust"' in reader.text

    response = client.post(
        "/cards",
        data={
            "csrf_token": csrf(response),
            "document_id": document_id,
            "page": "1",
            "target": "robust",
            "sentence": "This is a robust result.",
            "translations": "надёжный, устойчивый",
            "replacement": "надёжный",
            "alternatives": "durable, strong",
        },
        follow_redirects=True,
    )
    assert "Добавлены две карточки" in response.text

    token = csrf(response)
    exported = client.post("/export/csv", data={"csrf_token": token})
    assert exported.status_code == 200
    assert exported.data.startswith(b"\xef\xbb\xbf")
    assert exported.data.count(b"card::") == 2

    no_new = client.post("/export/csv", data={"csrf_token": token}, follow_redirects=True)
    assert "Новых карточек для CSV нет" in no_new.text
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT csv_exported_at FROM cards").fetchone()[0]


def test_users_cannot_open_each_others_documents(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    first = app.test_client()
    response = register(first, "first-user")
    first.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "private.pdf")},
        content_type="multipart/form-data",
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents").fetchone()[0]

    second = app.test_client()
    register(second, "second-user")
    assert second.get(f"/article/{document_id}").status_code == 404
    assert second.get(f"/file/pdf/{document_id}").status_code == 404
