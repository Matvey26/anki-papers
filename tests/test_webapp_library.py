from __future__ import annotations

import csv
import io
import sqlite3
import uuid
from pathlib import Path

from support_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.webapp as webapp_module


def test_register_upload_add_and_export_only_new_cards(
    tmp_path: Path, monkeypatch
) -> None:
    install_fake_enrichment(monkeypatch)
    app = make_app(tmp_path)
    client = app.test_client()
    response = identify(client)
    assert response.status_code == 200
    assert "Библиотека" in response.text
    assert "Выгрузка" not in response.text
    assert ">CSV<" not in response.text
    assert ">APKG<" not in response.text

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
    assert 'id="highlight-delete"' in reader.text
    assert "Добавить «${target}»" in "\n".join(path.read_text() for path in (Path(webapp_module.__file__).parent / "static").glob("reader*.js"))

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
    rows = list(
        csv.DictReader(io.StringIO(exported.data.decode("utf-8-sig")))
    )
    assert '>This is a <b>robust</b> result.</div><script>' in rows[0]["Front"]

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
    assert client.get(f"/api/article/{document_id}/highlights").json["highlights"] == []
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM deleted_highlights").fetchone()[0] == 1


def test_sync_moves_from_library_to_header_and_hides_profile_name(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client, "library-reader")

    assert 'class="sync-overview' not in dashboard.text
    assert "library-reader" not in dashboard.text
    assert ">Подключить AnkiWeb<" in dashboard.text

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        user_id = database.execute(
            "SELECT id FROM users WHERE username = 'library-reader'"
        ).fetchone()[0]
        database.execute(
            "INSERT INTO anki_accounts (user_id, state, updated_at) VALUES (?, 'connected', '2026-08-11T00:00:00+00:00')",
            (user_id,),
        )
        database.commit()

    dashboard = client.get("/dashboard")
    assert ">Синхронизировать<" in dashboard.text
    queued = client.post(
        "/settings/anki/sync",
        data={"csrf_token": csrf(dashboard), "next": "/dashboard"},
        follow_redirects=True,
    )
    assert "Синхронизация поставлена в очередь" in queued.text
    assert ">Синхронизация…<" in queued.text
    assert "library-reader" not in queued.text

    settings = client.get("/settings")
    assert "Профиль: library-reader" in settings.text


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
    assert second.delete(
        f"/api/article/{document_id}/highlights/{uuid.uuid4()}",
        headers={"X-CSRF-Token": csrf(second_dashboard)},
    ).status_code == 404
    assert second.get(f"/article/{document_id}/highlighted.pdf").status_code == 404
    assert second.post(
        f"/article/{document_id}/read",
        data={"csrf_token": csrf(second_dashboard), "read": "1"},
    ).status_code == 404
    assert second.post(
        f"/article/{document_id}/delete",
        data={"csrf_token": csrf(second_dashboard)},
    ).status_code == 404


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
    assert "Сохранённые слова" in response.text
    assert "Каждое слово создаёт две карточки Anki" in response.text
    assert response.text.count('class="saved-card"') == 6
    assert 'class="card-list is-collapsed"' in response.text
    assert "Показать все · 6" in response.text
    assert ">новое<" not in response.text
