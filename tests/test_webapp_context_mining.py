from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from support_webapp import csrf, identify, make_app, pdf_bytes

import articles_to_anki.cards.article_contexts as ref_cards_article_contexts
import articles_to_anki.cards.rendering as ref_cards_rendering
import articles_to_anki.documents.text_cache as ref_documents_text_cache
from articles_to_anki.extract import DocumentText


def test_uploaded_articles_add_meaning_only_context_without_highlight(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client)
    client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
    )
    database_path = tmp_path / "app.sqlite3"
    with sqlite3.connect(database_path) as database:
        document_id = database.execute("SELECT id FROM documents").fetchone()[0]
        source_context = {
            "id": "source-highlight",
            "source": "user_pdf",
            "target": "trails behind",
            "sentence": "The smaller model trails behind its peers in benchmark performance.",
            "replacement": "отстаёт от",
            "lemma": "trail behind",
            "family_key": "trail behind",
            "part_of_speech": "verb",
            "sense_definition_en": "to lag behind in performance",
        }
        database.execute(
            """INSERT INTO cards
               (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, lemma, family_key,
                part_of_speech, sense_definition_en, contexts_json, semantic_version, created_at)
               VALUES ('trail-card', 1, ?, 'trail behind', 'trail behind', ?, 1,
                       '["отставать"]', 'отстаёт', '[]', 'trail behind', 'trail behind',
                       'verb', 'to lag behind in performance', ?, 1, '2026-01-01')""",
            (document_id, source_context["sentence"], json.dumps([source_context])),
        )
        database.commit()

    article = (
        "However, open-source models considerably trail behind in benchmark performance."
    )
    parse_calls = 0

    def fake_extract_document_text(_path):
        nonlocal parse_calls
        parse_calls += 1
        return DocumentText(
            text=article,
            page_ranges=[(0, len(article))],
            hard_page_starts=[False],
        )

    monkeypatch.setattr(
        ref_documents_text_cache, "extract_document_text",
        fake_extract_document_text,
    )
    with sqlite3.connect(database_path) as database:
        database.row_factory = sqlite3.Row
        changed = ref_cards_article_contexts.refresh_article_contexts(database, 1)
        database.commit()
        card = database.execute("SELECT * FROM cards WHERE id = 'trail-card'").fetchone()
        contexts = json.loads(card["contexts_json"])
        assert changed == {"trail-card"}
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0
        assert contexts[-1]["source"] == "article_context"
        assert contexts[-1]["directions"] == ["meaning"]
        assert "considerably trail behind" in contexts[-1]["sentence"]

        assert ref_cards_article_contexts.refresh_article_contexts(database, 1) == set()
        assert parse_calls == 1
        text_path = database.execute(
            "SELECT text_path FROM documents WHERE id = ?", (document_id,)
        ).fetchone()[0]
        assert Path(text_path).is_file()
        rows = ref_cards_rendering.semantic_card_rows(card)
        assert "considerably &lt;b&gt;trail behind" in rows[0]["Front"]
        assert "considerably &lt;b&gt;trail behind" not in rows[1]["Front"]


def test_refresh_article_contexts_uses_llm_approval_for_variants(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client)
    client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
    )
    database_path = tmp_path / "app.sqlite3"
    with sqlite3.connect(database_path) as database:
        document_id = database.execute("SELECT id FROM documents").fetchone()[0]
        source_context = {
            "id": "source-highlight",
            "source": "user_pdf",
            "target": "impair",
            "sentence": "Chronic stress can impair memory consolidation.",
            "replacement": "ухудшать",
            "lemma": "impair",
            "family_key": "impair",
            "part_of_speech": "verb",
            "sense_definition_en": "to weaken or damage something",
        }
        database.execute(
            """INSERT INTO cards
               (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, lemma, family_key,
                part_of_speech, sense_definition_en, contexts_json, semantic_version, created_at)
               VALUES ('impair-card', 1, ?, 'impair', 'impair', ?, 1,
                       '["ухудшать"]', 'ухудшают', '[]', 'impair', 'impair',
                       'verb', 'to weaken or damage something', ?, 1, '2026-01-01')""",
            (document_id, source_context["sentence"], json.dumps([source_context])),
        )
        database.commit()

    article = (
        "Chronic stress can impair memory consolidation. "
        "An impaired immune response follows quickly in stressed animals. "
        "Обычный Russian sentence to block the variant behind it."
    )
    monkeypatch.setattr(
        ref_documents_text_cache, "extract_document_text",
        lambda _path: DocumentText(
            text=article,
            page_ranges=[(0, len(article))],
            hard_page_starts=[False],
        ),
    )
    sent_ids = []

    def fake_approve(*, candidates, **_kwargs):
        sent_ids.append([(item.id, item.surface) for item in candidates])
        return {item.id for item in candidates if item.surface == "impaired"}

    monkeypatch.setattr(ref_cards_article_contexts, "approve_context_candidates", fake_approve)
    with sqlite3.connect(database_path) as database:
        database.row_factory = sqlite3.Row
        changed = ref_cards_article_contexts.refresh_article_contexts(database, 1)
        database.commit()
        card = database.execute(
            "SELECT contexts_json FROM cards WHERE id = 'impair-card'"
        ).fetchone()
        contexts = json.loads(card["contexts_json"])

    assert changed == {"impair-card"}
    assert sent_ids[0][0][1] == "impaired"
    assert [item["target"] for item in contexts[1:]] == ["impaired"]
    assert "immune response" in contexts[1]["sentence"]


def test_refresh_article_contexts_falls_back_to_lexical_selection_on_approval_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client)
    client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
    )
    database_path = tmp_path / "app.sqlite3"
    with sqlite3.connect(database_path) as database:
        document_id = database.execute("SELECT id FROM documents").fetchone()[0]
        source_context = {
            "id": "source-highlight",
            "source": "user_pdf",
            "target": "impair",
            "sentence": "Chronic stress can impair memory consolidation.",
            "replacement": "ухудшать",
            "lemma": "impair",
            "family_key": "impair",
            "part_of_speech": "verb",
            "sense_definition_en": "to weaken or damage something",
        }
        database.execute(
            """INSERT INTO cards
               (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, lemma, family_key,
                part_of_speech, sense_definition_en, contexts_json, semantic_version, created_at)
               VALUES ('impair-card', 1, ?, 'impair', 'impair', ?, 1,
                       '["ухудшать"]', 'ухудшают', '[]', 'impair', 'impair',
                       'verb', 'to weaken or damage something', ?, 1, '2026-01-01')""",
            (document_id, source_context["sentence"], json.dumps([source_context])),
        )
        database.commit()

    article = "Chronic stress can impair memory consolidation drastically."
    monkeypatch.setattr(
        ref_documents_text_cache, "extract_document_text",
        lambda _path: DocumentText(
            text=article,
            page_ranges=[(0, len(article))],
            hard_page_starts=[False],
        ),
    )

    def failing_approve(**_kwargs):
        raise OSError("provider unavailable")

    monkeypatch.setattr(ref_cards_article_contexts, "approve_context_candidates", failing_approve)
    with sqlite3.connect(database_path) as database:
        database.row_factory = sqlite3.Row
        changed = ref_cards_article_contexts.refresh_article_contexts(database, 1)
        database.commit()
        card = database.execute(
            "SELECT contexts_json FROM cards WHERE id = 'impair-card'"
        ).fetchone()
        contexts = json.loads(card["contexts_json"])

    assert changed == {"impair-card"}
    assert contexts[1]["source"] == "article_context"
    assert "drastically" in contexts[1]["sentence"]
