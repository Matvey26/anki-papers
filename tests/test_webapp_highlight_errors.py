from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from support_webapp import csrf, identify, make_app, pdf_bytes

import articles_to_anki.cards.llm_cache as ref_cards_llm_cache


def test_highlight_is_silently_discarded_when_translation_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def failed_enrich(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(ref_cards_llm_cache, "analyse_cluster_assignment", failed_enrich)
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
    assert response.json["discarded_highlight_id"]
    stored = client.get(f"/api/article/{document_id}/highlights").json["highlights"]
    assert stored == []
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM deleted_highlights").fetchone()[0] == 1
