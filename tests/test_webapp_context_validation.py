from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from support_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.cards.context_selection as ref_cards_context_selection
import articles_to_anki.cards.enrichment as ref_cards_enrichment


def test_purge_llm_generated_contexts_removes_legacy_synthetic_contexts(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    identify(app.test_client())
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute(
            """INSERT INTO documents
               (id, user_id, kind, name, stored_path, page_count, size, created_at)
               VALUES ('doc', 1, 'pdf', 'paper.pdf', '/tmp/paper.pdf', 1, 1, '2026-01-01')"""
        )
        real_context = {
            "id": "source:highlight",
            "source": "user_pdf",
            "target": "impair",
            "sentence": "An impaired immune response follows quickly.",
        }
        synthetic_context = {
            "id": "generated:legacy",
            "source": "llm_generated",
            "target": "impaired",
            "sentence": "Even mild radiation can impair memory formation over time.",
        }
        database.execute(
            """INSERT INTO cards
               (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, lemma, family_key,
                part_of_speech, sense_definition_en, contexts_json, semantic_version, created_at)
               VALUES ('legacy-card', 1, 'doc', 'impair', 'impair', 'Chronic stress can impair memory.', 1,
                       '["ухудшать"]', 'ухудшают', '[]', 'impair', 'impair',
                       'verb', 'to weaken or damage something', ?, 1, '2026-01-01')""",
            (json.dumps([real_context, synthetic_context]),),
        )
        database.commit()

    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.row_factory = sqlite3.Row
        contexts = json.loads(
            database.execute(
                "SELECT contexts_json FROM cards WHERE id = 'legacy-card'"
            ).fetchone()[0]
        )

    assert [item["source"] for item in contexts] == ["user_pdf"]


def test_article_context_mining_runs_after_ready_commit(
    tmp_path: Path,
    monkeypatch,
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

    saw_committed_ready = False

    def fail_after_check(_database, _user_id, **_kwargs):
        nonlocal saw_committed_ready
        with sqlite3.connect(tmp_path / "app.sqlite3") as observer:
            status = observer.execute("SELECT status FROM highlights").fetchone()[0]
            card_count = observer.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        saw_committed_ready = status == "ready" and card_count == 1
        raise RuntimeError("context index unavailable")

    monkeypatch.setattr(ref_cards_enrichment, "refresh_article_contexts", fail_after_check)
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
    assert response.json["highlight"]["status"] == "ready"
    assert saw_committed_ready
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT status FROM highlights").fetchone()[0] == "ready"
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1


def test_article_context_rejects_toc_and_numeric_table_rows() -> None:
    assert not ref_cards_context_selection._article_context_is_readable(
        "7 2.1.2 DeepSeekMoE with Auxiliary-Loss-Free Load Balancing ."
    )
    assert not ref_cards_context_selection._article_context_is_readable(
        "… Annotator 1 100.0% Annotator 2 66.7% Annotator 3 59.8% and 42.1%."
    )
    assert not ref_cards_context_selection._article_context_is_readable(
        "Figure 3 shows stronger results than Figure 3| Benchmark curves for several corpora."
    )
    assert ref_cards_context_selection._article_context_is_readable(
        "In engineering tasks, DeepSeek-V3 trails behind Claude-Sonnet but outperforms open models."
    )
