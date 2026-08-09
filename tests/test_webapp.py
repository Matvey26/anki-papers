from __future__ import annotations

import io
import re
import sqlite3
import uuid
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight, Text
from pypdf.generic import ArrayObject, FloatObject

import articles_to_anki.webapp as webapp_module
from articles_to_anki.extract import ExtractedHighlight
from articles_to_anki.models import EnrichedItem
from articles_to_anki.webapp import create_app, make_target_context


def csrf(response) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data) or re.search(
        rb'window\.ANKI_PAPERS_CSRF = "([^"]+)"', response.data
    )
    assert match
    return match.group(1).decode()


def pdf_bytes() -> io.BytesIO:
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.write(stream)
    stream.seek(0)
    return stream


def make_app(tmp_path: Path, **config):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATA_DIR": tmp_path,
            "DATABASE": tmp_path / "app.sqlite3",
            "AUTO_PROCESS_UPLOADS": False,
            "PROCESS_DOCUMENTS_INLINE": True,
            **config,
        }
    )


def identify(client, username: str = "reader"):
    token = csrf(client.get("/login"))
    return client.post(
        "/login",
        data={"csrf_token": token, "username": username},
        follow_redirects=True,
    )


def install_fake_enrichment(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def fake_enrich(targets, **_kwargs):
        assert targets[0].target == "robust"
        assert targets[0].source_page == 1
        return [
            EnrichedItem(
                id=targets[0].id,
                context_explanation_ru="Значение подтверждается контекстом.",
                translations_ru=["надёжный", "устойчивый"],
                replacement_ru="надёжный",
                forbidden_alternatives_en=["durable", "strong"],
            )
        ]

    monkeypatch.setattr(webapp_module, "enrich_targets", fake_enrich)


def test_register_upload_add_and_export_only_new_cards(
    tmp_path: Path, monkeypatch
) -> None:
    install_fake_enrichment(monkeypatch)
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
    assert 'id="card-dialog"' not in reader.text
    assert 'id="selection-action"' in reader.text
    assert "Добавить «${target}»" in (Path(webapp_module.__file__).parent / "static" / "reader.js").read_text()

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
    assert response.json["highlight"]["status"] == "ready"

    token = csrf(client.get("/dashboard"))
    exported = client.post("/export/csv", data={"csrf_token": token})
    assert exported.status_code == 200
    assert exported.data.startswith(b"\xef\xbb\xbf")
    assert exported.data.count(b"card::") == 2

    no_new = client.post("/export/csv", data={"csrf_token": token}, follow_redirects=True)
    assert "Новых карточек для CSV нет" in no_new.text
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT csv_exported_at FROM cards").fetchone()[0]
        card_id = database.execute("SELECT id FROM cards").fetchone()[0]
    deleted = client.post(
        f"/cards/{card_id}/delete",
        data={"csrf_token": csrf(client.get("/dashboard"))},
        follow_redirects=True,
    )
    assert "Карточка удалена" not in deleted.text


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
    assert second.get(f"/api/article/{document_id}/highlights").status_code == 404
    assert second.get(f"/article/{document_id}/highlighted.pdf").status_code == 404
    assert second.post(
        f"/article/{document_id}/read",
        data={"csrf_token": csrf(second_dashboard), "read": "1"},
    ).status_code == 404
    assert second.post(
        f"/article/{document_id}/delete",
        data={"csrf_token": csrf(second_dashboard)},
    ).status_code == 404


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
    assert ">Прочитано<" in dashboard.text
    download = client.get(f"/article/{document_id}/highlighted.pdf")
    assert download.status_code == 200
    assert download.headers["Content-Disposition"].startswith("attachment;")
    highlighted = PdfReader(io.BytesIO(download.data))
    annotation = highlighted.pages[0]["/Annots"][0].get_object()
    assert annotation["/Subtype"] == "/Highlight"
    assert "robust: надёжный, устойчивый" == annotation["/Contents"]


def test_highlight_stays_saved_when_translation_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def failed_enrich(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(webapp_module, "enrich_targets", failed_enrich)
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
    assert response.status_code == 502
    assert response.json["highlight"]["status"] == "failed"
    stored = client.get(f"/api/article/{document_id}/highlights").json["highlights"]
    assert len(stored) == 1
    assert stored[0]["target"] == "robust"


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
    monkeypatch.setattr(webapp_module, "extract_highlights", lambda _path: [extracted])
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
        assert highlight["source"] == "pdf_import"
        assert highlight["status"] == "ready"
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1


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
    monkeypatch.setattr(webapp_module, "extract_highlights", lambda _path: [extracted])
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

    webapp_module.process_document_highlights(app, document_id, user_id)
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


def test_clean_pdf_removes_only_native_highlight_annotations(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    destination = tmp_path / "working.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
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

    webapp_module.write_pdf_without_native_highlights(source, destination)

    annotations = [
        reference.get_object()["/Subtype"]
        for reference in PdfReader(destination).pages[0]["/Annots"]
    ]
    assert annotations == ["/Text"]


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


def test_recent_cards_are_collapsed_after_five(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client)
    client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
        document_id = database.execute("SELECT id FROM documents").fetchone()[0]
        database.executemany(
            """INSERT INTO cards
               (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, '[\"слово\", \"вариант\"]',
                       'слово', '[\"plain\", \"simple\"]', ?)""",
            [
                (
                    str(uuid.uuid4()),
                    user_id,
                    document_id,
                    f"word-{index}",
                    f"word-{index}",
                    f"Sentence {index}.",
                    f"2026-08-09T00:00:0{index}+00:00",
                )
                for index in range(6)
            ],
        )
        database.commit()

    response = client.get("/dashboard")
    assert "Недавние сохранённые карточки" in response.text
    assert response.text.count('class="saved-card"') == 6
    assert 'class="card-list is-collapsed"' in response.text
    assert "Показать все · 6" in response.text
    assert ">новое<" not in response.text
