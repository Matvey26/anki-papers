from __future__ import annotations

import base64
import io
import os
import sqlite3
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support_rebuild import (
    _KEY,
    _add_highlight,
    _install_ankiweb_mirror,
    _read_rebuilt,
    _site_card_id,
    _start_rebuild,
)
from support_rebuild_fixtures import _rebuild_download
from support_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.jobs.rebuild as ref_jobs_rebuild
import articles_to_anki.rebuild as ref_rebuild


def test_rebuild_import_keeps_notes_out_of_regular_reconcile(
    tmp_path: Path, monkeypatch
) -> None:
    if os.environ.get("RUN_ANKI_SYNC_INTEGRATION") != "1":
        pytest.skip("set RUN_ANKI_SYNC_INTEGRATION=1")
    pytest.importorskip("anki")
    from anki.collection import Collection
    from anki.import_export_pb2 import (
        ImportAnkiPackageOptions,
        ImportAnkiPackageRequest,
    )
    from anki_papers_sync_worker.official import OfficialAnkiAdapter

    install_fake_enrichment(monkeypatch)
    monkeypatch.setattr(
        ref_rebuild, "_deck_name",
        lambda value: "Anki Papers (пересборка) 2026-01-01 00:00",
    )
    app = make_app(tmp_path, REBUILD_INLINE=True)
    client = app.test_client()
    response = identify(client)

    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents WHERE kind = 'pdf'").fetchone()[0]
    client.post(f"/article/{document_id}/read", data={"csrf_token": csrf(response), "read": "1"})
    _add_highlight(client, document_id)
    site_id = _site_card_id(tmp_path)
    _start_rebuild(client, tmp_path)
    _payload, download = _rebuild_download(client)

    apkg = tmp_path / "rebuilt.apkg"
    apkg.write_bytes(download.data)
    destination = tmp_path / "imported.anki2"
    collection = Collection(str(destination))
    try:
        collection.import_anki_package(
            ImportAnkiPackageRequest(
                package_path=str(apkg.resolve()),
                options=ImportAnkiPackageOptions(
                    merge_notetypes=True,
                    with_scheduling=True,
                    with_deck_configs=False,
                ),
            )
        )
        notes = [collection.get_note(note_id) for note_id in collection.find_notes("")]
        assert len(notes) == 2
        assert all("rebuild" in note.tags for note in notes)
        adapter = OfficialAnkiAdapter()
        assert adapter._find_note(collection, {"id": site_id}, "meaning", set()) is None
        assert adapter._find_note(collection, {"id": site_id}, "recall", set()) is None
    finally:
        collection.close()


def test_rebuild_job_persists_upload_queue_when_connection_closes(
    tmp_path: Path, monkeypatch
) -> None:
    """The production background thread closes its own database connection; the
    rebuild_import sync job must survive that close (regression: the enqueue
    insert was rolled back by the implicit close and the worker never saw it)."""
    install_fake_enrichment(monkeypatch)
    monkeypatch.setattr(
        ref_rebuild, "_deck_name",
        lambda value: "Anki Papers (пересборка) 2026-01-01 00:00",
    )
    monkeypatch.setenv("ANKI_CREDENTIAL_KEY", base64.urlsafe_b64encode(_KEY).decode())
    app = make_app(tmp_path, REBUILD_INLINE=True)
    client = app.test_client()
    response = identify(client)

    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents WHERE kind = 'pdf'").fetchone()[0]
    client.post(f"/article/{document_id}/read", data={"csrf_token": csrf(response), "read": "1"})
    _add_highlight(client, document_id)
    site_id = _site_card_id(tmp_path)
    _install_ankiweb_mirror(tmp_path, site_id)

    job_id = str(uuid.uuid4())
    timestamp = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
        database.execute(
            """INSERT INTO rebuild_jobs
               (id, user_id, state, deck_id, progress, stage, error, result_path,
                uploaded_to, created_at, started_at, finished_at, updated_at)
               VALUES (?, ?, 'queued', 2, 0, '', NULL, NULL, NULL, ?, NULL, NULL, ?)""",
            (job_id, user_id, timestamp, timestamp),
        )
        database.commit()

    ref_jobs_rebuild.run_rebuild_job(app, job_id, user_id, 2)

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        job = database.execute(
            "SELECT reason, state FROM sync_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        rebuild_row = database.execute(
            "SELECT state, uploaded_to FROM rebuild_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        result_path = database.execute(
            "SELECT result_path FROM rebuild_jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
    assert job == ("rebuild_import", "queued")
    assert rebuild_row == ("succeeded", "queued")
    assert result_path is not None
    assert Path(result_path).is_file()


def test_settings_shows_running_rebuild_job_panel(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    app = make_app(tmp_path)
    client = app.test_client()
    identify(client)
    timestamp = datetime.now(UTC).isoformat()
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
        database.execute(
            """INSERT INTO rebuild_jobs
               (id, user_id, state, deck_id, progress, stage, error, result_path,
                created_at, started_at, finished_at, updated_at)
               VALUES ('job-1', ?, 'running', NULL, 35, 'Контексты предложений', '',
                       NULL, ?, ?, NULL, ?)""",
            (user_id, timestamp, timestamp, timestamp),
        )
        database.commit()

    page = client.get("/settings")
    assert page.status_code == 200
    assert 'data-rebuild-job' in page.text
    assert 'data-job-id="job-1"' in page.text
    assert 'data-state="running"' in page.text
    assert 'data-progress="35"' in page.text
    assert 'src="{{ url_for(' not in page.text
    assert "rebuild-status.js" in page.text
    assert "disabled" in page.text

    status = client.get("/api/rebuild/status")
    payload = status.get_json()["rebuild_job"]
    assert payload["state"] == "running"
    assert payload["progress"] == 35
    assert payload["stage"] == "Контексты предложений"
    assert payload["download_url"] is None


def test_rebuild_job_runs_in_background_and_survives_page_reload(
    tmp_path: Path, monkeypatch
) -> None:
    install_fake_enrichment(monkeypatch)
    monkeypatch.setattr(
        ref_rebuild, "_deck_name",
        lambda value: "Anki Papers (пересборка) 2026-01-01 00:00",
    )
    app = make_app(tmp_path)
    client = app.test_client()
    response = identify(client)

    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents WHERE kind = 'pdf'").fetchone()[0]
    client.post(f"/article/{document_id}/read", data={"csrf_token": csrf(response), "read": "1"})
    _add_highlight(client, document_id)

    _start_rebuild(client, tmp_path)

    deadline = time.monotonic() + 30
    payload: dict | None = None
    while time.monotonic() < deadline:
        status = client.get("/api/rebuild/status")
        payload = status.get_json()["rebuild_job"]
        assert payload is not None
        state = payload["state"]
        assert state in {"queued", "running", "succeeded"}
        if state == "succeeded":
            break
        time.sleep(0.1)
    assert payload is not None and payload["state"] == "succeeded"
    assert payload["progress"] == 100
    assert 0 <= payload["progress"] <= 100

    reloaded = client.get("/settings")
    assert "data-state=\"succeeded\"" in reloaded.text
    assert 'data-download-url="' in reloaded.text

    download = client.get(payload["download_url"])
    assert download.status_code == 200
    assert download.data.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
        assert len(archive.namelist()) >= 2


def test_rebuild_notes_are_tagged_and_upload_queued_when_connected(
    tmp_path: Path, monkeypatch
) -> None:
    install_fake_enrichment(monkeypatch)
    monkeypatch.setattr(
        ref_rebuild, "_deck_name",
        lambda value: "Anki Papers (пересборка) 2026-01-01 00:00",
    )
    monkeypatch.setenv("ANKI_CREDENTIAL_KEY", base64.urlsafe_b64encode(_KEY).decode())
    app = make_app(tmp_path, REBUILD_INLINE=True)
    client = app.test_client()
    response = identify(client)

    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents WHERE kind = 'pdf'").fetchone()[0]
    client.post(f"/article/{document_id}/read", data={"csrf_token": csrf(response), "read": "1"})
    _add_highlight(client, document_id)
    site_id = _site_card_id(tmp_path)
    _install_ankiweb_mirror(tmp_path, site_id)

    _start_rebuild(client, tmp_path, deck_id="2")
    payload, download = _rebuild_download(client)
    assert payload["state"] == "succeeded"
    assert payload["auto_upload"] is True

    connection = _read_rebuilt(tmp_path, download)
    try:
        rows = connection.execute("SELECT tags FROM notes").fetchall()
        assert len(rows) == 2
        assert all(" rebuild " in row[0] for row in rows)
    finally:
        connection.close()

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        job = database.execute(
            "SELECT reason, state FROM sync_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert job == ("rebuild_import", "queued")
        rebuild_row = database.execute(
            "SELECT state, result_path FROM rebuild_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert rebuild_row[0] == "succeeded"
        assert rebuild_row[1] is not None


def test_rebuild_without_ankiweb_keeps_download_only(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    app = make_app(tmp_path, REBUILD_INLINE=True)
    client = app.test_client()
    response = identify(client)

    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id = database.execute("SELECT id FROM documents WHERE kind = 'pdf'").fetchone()[0]
    client.post(f"/article/{document_id}/read", data={"csrf_token": csrf(response), "read": "1"})
    _add_highlight(client, document_id)

    _start_rebuild(client, tmp_path)
    payload, _download = _rebuild_download(client)
    assert payload["state"] == "succeeded"
    assert payload["auto_upload"] is False

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        jobs = database.execute("SELECT reason, state FROM sync_jobs").fetchall()
        assert all(row[0] != "rebuild_import" for row in jobs)
