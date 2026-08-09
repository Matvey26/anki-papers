from __future__ import annotations

import io
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


def identify(client, username: str = "reader"):
    token = csrf(client.get("/login"))
    return client.post(
        "/login",
        data={"csrf_token": token, "username": username},
        follow_redirects=True,
    )


def test_register_upload_add_and_export_only_new_cards(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    response = identify(client)
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
        document_id = database.execute(
            "SELECT id FROM documents WHERE kind = 'pdf'"
        ).fetchone()[0]
    reader = client.get(f"/article/{document_id}")
    assert 'id="pdf-workspace"' in reader.text
    assert f'/file/pdf/{document_id}' in reader.text
    assert "Оригинал PDF" not in reader.text
    assert ">Текст<" not in reader.text

    response = client.post(
        f"/article/{document_id}/read",
        data={"csrf_token": csrf(response), "read": "1"},
        follow_redirects=True,
    )
    assert "Статья отмечена прочитанной" in response.text
    assert "· Прочитано" in response.text
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT read_at FROM documents WHERE id = ?", (document_id,)).fetchone()[0]

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
    response = identify(first, "first-user")
    first.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "private.pdf")},
        content_type="multipart/form-data",
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents").fetchone()[0]

    second = app.test_client()
    second_dashboard = identify(second, "second-user")
    assert second.get(f"/article/{document_id}").status_code == 404
    assert second.get(f"/file/pdf/{document_id}").status_code == 404
    assert second.post(
        f"/article/{document_id}/read",
        data={"csrf_token": csrf(second_dashboard), "read": "1"},
    ).status_code == 404


def test_existing_username_logs_into_same_profile_without_password(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    first = app.test_client()
    auth_page = first.get("/register")
    assert 'name="password"' not in auth_page.text
    assert "профиль создастся автоматически" in auth_page.text
    response = identify(first, "same-user")
    first.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "saved.pdf")},
        content_type="multipart/form-data",
    )

    second = app.test_client()
    response = identify(second, "SAME-user")
    assert "saved.pdf" in response.text
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_existing_database_drops_obsolete_password_hash(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    identify(app.test_client(), "old-user")
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute(
            "ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT 'old-hash'"
        )

    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(users)")}
        username = database.execute("SELECT username FROM users").fetchone()[0]
    assert "password_hash" not in columns
    assert username == "old-user"


def test_existing_database_gets_read_at_migration(tmp_path: Path) -> None:
    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute("ALTER TABLE documents DROP COLUMN read_at")
    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(documents)")}
    assert "read_at" in columns
