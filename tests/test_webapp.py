from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import uuid
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight, Text
from pypdf.generic import ArrayObject, DecodedStreamObject, FloatObject

import articles_to_anki.apkg as apkg_module
import articles_to_anki.webapp as webapp_module
from articles_to_anki.extract import ExtractedHighlight
from articles_to_anki.models import EnrichedItem, SemanticAnalysis, SemanticMatch
from articles_to_anki.security import claim_token_digest
from articles_to_anki.webapp import create_app, make_target_context


def csrf(response) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data) or re.search(
        rb'window\.ANKI_PAPERS_CSRF = "([^"]+)"', response.data
    )
    assert match
    return match.group(1).decode()


def pdf_bytes(page_count: int = 1) -> io.BytesIO:
    stream = io.BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
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
    password = "correct horse battery staple"
    token = csrf(client.get("/register"))
    return client.post(
        "/register",
        data={
            "csrf_token": token,
            "username": username,
            "password": password,
            "password_confirmation": password,
        },
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

    def fake_semantic(target, sentence, **_kwargs):
        assert target == "robust"
        return SemanticAnalysis(
            lemma="robust",
            family_key="robust",
            part_of_speech="adjective",
            sense_definition_en="reliable and resilient in operation",
            translations_ru=["надёжный", "устойчивый"],
            replacement_ru="надёжный",
            generated_sentence="The method remained robust under a substantial distribution shift.",
            generated_surface="robust",
            generated_translation_ru="устойчивым",
            source_distractors={
                "substitutes_en": ["strong", "durable"],
                "related_en": ["strength", "resilience"],
                "valid_substitutes_en": ["strong"],
                "valid_related_en": ["strength"],
            },
            generated_distractors={
                "substitutes_en": ["reliable"],
                "related_en": ["reliability"],
                "valid_substitutes_en": ["reliable"],
                "valid_related_en": ["reliability"],
            },
        )

    monkeypatch.setattr(webapp_module, "analyse_semantic_context", fake_semantic)
    def fake_match(analysis, candidates, **_kwargs):
        if not candidates:
            return SemanticMatch(
                card_id=None,
                relationship="new_card",
                merged_sense_definition_en=None,
                rationale_ru="Кандидатов пока нет.",
            )
        return SemanticMatch(
            card_id=candidates[0].id,
            relationship="same_sense",
            merged_sense_definition_en=analysis.sense_definition_en,
            rationale_ru="Это одно значение.",
        )

    monkeypatch.setattr(webapp_module, "select_semantic_match", fake_match)


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


def test_quick_translation_returns_part_of_speech_groups(tmp_path: Path, monkeypatch) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    identify(client)
    monkeypatch.setattr(
        webapp_module,
        "quick_translation_groups",
        lambda word: [{"part_of_speech": "гл.", "translations": [word, "мчаться"]}],
    )

    response = client.get("/api/quick-translation?word=run")

    assert response.status_code == 200
    assert response.json == {
        "groups": [{"part_of_speech": "гл.", "translations": ["run", "мчаться"]}]
    }
    assert client.get("/api/quick-translation?word=two%20words").status_code == 200
    assert client.get("/api/quick-translation?word=one%20two%20three").status_code == 200
    assert client.get("/api/quick-translation?word=one%20two%20three%20four").status_code == 400


def test_selected_text_normalizes_line_break_hyphens_and_keeps_regular_hyphens() -> None:
    assert webapp_module.normalize_selected_text("inter-\nnational") == "international"
    assert webapp_module.normalize_selected_text("well-known") == "well-known"
    assert webapp_module.is_selectable_target("rule out")
    assert webapp_module.is_selectable_target("one two three")
    assert not webapp_module.is_selectable_target("one two three four")


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


def test_same_word_in_same_sense_merges_contexts(
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
    for index, sentence in enumerate(
        ("This is a robust result.", "We need a robust implementation."), start=1
    ):
        response = client.post(
            f"/api/article/{document_id}/highlights",
            json={
                "id": str(uuid.uuid4()),
                "target": "robust",
                "sentence": sentence,
                "page": 1,
                "rects": [{"x1": 80, "y1": 180 + index * 20, "x2": 120, "y2": 195 + index * 20}],
            },
            headers={"X-CSRF-Token": csrf(reader)},
        )
        assert response.status_code == 200

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.row_factory = sqlite3.Row
        card = database.execute(
            "SELECT contexts_json, semantic_version FROM cards WHERE target_normalized = 'robust'"
        ).fetchone()
        assert card[1] == 1
        contexts = json.loads(card[0])
        assert len(contexts) == 4
        assert contexts[0]["substitutes_en"] == ["strong", "durable"]
        assert contexts[0]["related_en"] == ["strength", "resilience"]
        assert contexts[0]["valid_substitutes_en"] == ["strong"]
        assert contexts[0]["valid_related_en"] == ["strength"]
        full_card = database.execute(
            "SELECT * FROM cards WHERE target_normalized = 'robust'"
        ).fetchone()
    rows = list(
        csv.DictReader(
            io.StringIO(webapp_module.cards_to_csv([full_card]).decode("utf-8-sig"))
        )
    )
    assert "Подходит, но не целевой ответ: strong" in rows[1]["Front"]
    assert "Близко по смыслу: strength" in rows[1]["Front"]
    assert "durable" not in rows[1]["Front"]
    assert "resilience" not in rows[1]["Front"]


def test_lexical_family_merges_derivations_but_splits_polysemy(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    analyses = {
        "acquired": SemanticAnalysis(
            lemma="acquire",
            family_key="acquire",
            part_of_speech="verb",
            sense_definition_en="obtain or gain possession of something",
            translations_ru=["приобрести", "получить"],
            replacement_ru="приобрела",
            generated_sentence="The laboratory acquired a more precise sensor.",
            generated_surface="acquired",
            generated_translation_ru="приобрела",
            source_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
            generated_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
        ),
        "acquisition": SemanticAnalysis(
            lemma="acquisition",
            family_key="acquisition",
            part_of_speech="noun",
            sense_definition_en="the act or process of obtaining something",
            translations_ru=["приобретение", "получение"],
            replacement_ru="получение",
            generated_sentence="Data acquisition requires careful calibration.",
            generated_surface="acquisition",
            generated_translation_ru="сбор",
            source_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
            generated_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
        ),
        "recognize-identify": SemanticAnalysis(
            lemma="recognize",
            family_key="recognize",
            part_of_speech="verb",
            sense_definition_en="identify something from previous knowledge",
            translations_ru=["распознать", "узнать"],
            replacement_ru="распознать",
            generated_sentence="The model can recognize partially hidden symbols.",
            generated_surface="recognize",
            generated_translation_ru="распознавать",
            source_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
            generated_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
        ),
        "recognize-admit": SemanticAnalysis(
            lemma="recognize",
            family_key="recognize",
            part_of_speech="verb",
            sense_definition_en="admit or acknowledge that something is true",
            translations_ru=["признать", "признавать"],
            replacement_ru="признать",
            generated_sentence="The review must recognize the study's limitations.",
            generated_surface="recognize",
            generated_translation_ru="признать",
            source_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
            generated_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
        ),
    }

    def fake_analysis(target, sentence, **_kwargs):
        if target != "recognize":
            return analyses[target]
        key = "recognize-identify" if "face" in sentence else "recognize-admit"
        return analyses[key]

    def fake_match(analysis, candidates, **_kwargs):
        if not candidates:
            return SemanticMatch(
                card_id=None,
                relationship="new_card",
                merged_sense_definition_en=None,
                rationale_ru="Подходящей карточки пока нет.",
            )
        if analysis.family_key.startswith("acqui"):
            return SemanticMatch(
                card_id=candidates[0].id,
                relationship="related_sense",
                merged_sense_definition_en="obtain something or the process of obtaining it",
                rationale_ru="Формы разделяют смысловое ядро получения.",
            )
        return SemanticMatch(
            card_id=None,
            relationship="new_card",
            merged_sense_definition_en=None,
            rationale_ru="Значения распознавания и признания различаются.",
        )

    monkeypatch.setattr(webapp_module, "analyse_semantic_context", fake_analysis)
    monkeypatch.setattr(webapp_module, "select_semantic_match", fake_match)
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
    highlights = (
        ("acquired", "The team acquired a useful dataset."),
        ("acquisition", "The acquisition of data took several weeks."),
        ("recognize", "Humans recognize a familiar face quickly."),
        ("recognize", "We recognize that the estimate is uncertain."),
    )
    for index, (target, sentence) in enumerate(highlights):
        response = client.post(
            f"/api/article/{document_id}/highlights",
            json={
                "id": str(uuid.uuid4()),
                "target": target,
                "sentence": sentence,
                "page": 1,
                "rects": [
                    {
                        "x1": 80,
                        "y1": 180 + index * 20,
                        "x2": 140,
                        "y2": 195 + index * 20,
                    }
                ],
            },
            headers={"X-CSRF-Token": csrf(reader)},
        )
        assert response.status_code == 200

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.row_factory = sqlite3.Row
        cards = database.execute(
            "SELECT * FROM cards ORDER BY created_at"
        ).fetchall()
    acquire_cards = [card for card in cards if card["family_key"] == "acquire"]
    recognize_cards = [card for card in cards if card["family_key"] == "recognize"]
    assert len(cards) == 3
    assert len(acquire_cards) == 1
    assert len(recognize_cards) == 2
    acquire_contexts = json.loads(acquire_cards[0]["contexts_json"])
    assert {context["lemma"] for context in acquire_contexts} == {
        "acquire",
        "acquisition",
    }
    assert json.loads(acquire_cards[0]["translations_json"]) == [
        "приобрести",
        "получить",
        "приобретение",
        "получение",
    ]
    assert acquire_cards[0]["sense_definition_en"] == (
        "obtain something or the process of obtaining it"
    )
    exported_rows = list(
        csv.DictReader(
            io.StringIO(
                webapp_module.cards_to_csv(acquire_cards).decode("utf-8-sig")
            )
        )
    )
    assert len(exported_rows) == 2
    assert "anki-papers-semantic-answer" in exported_rows[1]["Back"]
    assert "acquired" in exported_rows[1]["Back"]
    assert "acquisition" in exported_rows[1]["Back"]
    assert "[...]" not in exported_rows[1]["Front"]
    assert "приобрела" in exported_rows[1]["Front"]
    assert "получение" in exported_rows[1]["Front"]
    assert "item.source" not in exported_rows[1]["Front"]
    assert "item.translation" not in exported_rows[1]["Front"]
    assert "family:" not in exported_rows[1]["Back"]


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


def test_highlight_is_silently_discarded_when_translation_fails(
    tmp_path: Path, monkeypatch
) -> None:
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
    assert response.status_code == 200
    assert response.json["discarded_highlight_id"]
    stored = client.get(f"/api/article/{document_id}/highlights").json["highlights"]
    assert stored == []
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM deleted_highlights").fetchone()[0] == 1


def test_import_failure_is_silent_and_does_not_leave_dead_highlight(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
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
    monkeypatch.setattr(
        webapp_module,
        "enrich_targets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )
    app = make_app(tmp_path, AUTO_PROCESS_UPLOADS=True)
    client = app.test_client()
    dashboard = identify(client)
    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert "Не удалось подготовить переводов" not in response.text
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document = database.execute(
            """SELECT highlight_status, highlight_error, imported_highlight_count
               FROM documents"""
        ).fetchone()
        assert document == ("ready", None, 0)
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM deleted_highlights").fetchone()[0] == 1


def test_existing_passwordless_profile_requires_one_time_claim(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute(
            "INSERT INTO users(username, created_at) VALUES ('same-user', '2026-01-01')"
        )
        user_id = database.execute(
            "SELECT id FROM users WHERE username = 'same-user'"
        ).fetchone()[0]
        database.execute(
            """INSERT INTO account_claim_tokens
               (id, user_id, token_hash, expires_at, created_at)
               VALUES ('claim-1', ?, ?, '2099-01-01', '2026-01-01')""",
            (user_id, claim_token_digest("one-time-secret")),
        )

    client = app.test_client()
    login = client.get("/login")
    rejected = client.post(
        "/login",
        data={"csrf_token": csrf(login), "username": "same-user", "password": "anything-long"},
        follow_redirects=True,
    )
    assert "claim-код" in rejected.text
    claim = client.get("/claim")
    claimed = client.post(
        "/claim",
        data={
            "csrf_token": csrf(claim),
            "username": "SAME-user",
            "claim_code": "one-time-secret",
            "password": "new secure password",
            "password_confirmation": "new secure password",
        },
        follow_redirects=True,
    )
    assert "Библиотека" in claimed.text
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute(
            "SELECT used_at IS NOT NULL FROM account_claim_tokens"
        ).fetchone()[0] == 1


def test_existing_database_adds_nullable_password_hash(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute(
            """CREATE TABLE users (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               username TEXT NOT NULL COLLATE NOCASE UNIQUE,
               created_at TEXT NOT NULL)"""
        )
        database.execute(
            "INSERT INTO users(username, created_at) VALUES ('old-user', '2026-01-01')"
        )

    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(users)")}
        username = database.execute("SELECT username FROM users").fetchone()[0]
    assert "password_hash" in columns
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        password_hash = database.execute("SELECT password_hash FROM users").fetchone()[0]
    assert username == "old-user"
    assert password_hash is None


def test_legacy_passwordless_session_is_forced_to_claim(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute(
            "INSERT INTO users(username, created_at) VALUES ('legacy', '2026-01-01')"
        )
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["user_id"] = user_id
        browser_session["username"] = "legacy"
    response = client.get("/dashboard", follow_redirects=True)
    assert "Claim-код" in response.text
    with client.session_transaction() as browser_session:
        assert "user_id" not in browser_session


def test_registration_requires_long_password_and_secure_cookie(tmp_path: Path) -> None:
    app = make_app(tmp_path, SESSION_COOKIE_SECURE=True)
    client = app.test_client()
    page = client.get("/register")
    rejected = client.post(
        "/register",
        data={
            "csrf_token": csrf(page),
            "username": "secure-user",
            "password": "too-short",
            "password_confirmation": "too-short",
        },
        follow_redirects=True,
    )
    assert "не менее 12" in rejected.text
    page = client.get("/register")
    accepted = client.post(
        "/register",
        data={
            "csrf_token": csrf(page),
            "username": "secure-user",
            "password": "long secure password",
            "password_confirmation": "long secure password",
        },
    )
    cookie = accepted.headers["Set-Cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_login_rate_limit_uses_username_or_ip(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(webapp_module.time, "sleep", lambda _seconds: None)
    app = make_app(tmp_path)
    identify(app.test_client(), "rate-user")
    attacker = app.test_client()
    for _ in range(8):
        page = attacker.get("/login")
        attacker.post(
            "/login",
            data={
                "csrf_token": csrf(page),
                "username": "rate-user",
                "password": "incorrect password",
            },
        )
    page = attacker.get("/login")
    limited = attacker.post(
        "/login",
        data={
            "csrf_token": csrf(page),
            "username": "different-user",
            "password": "incorrect password",
        },
    )
    assert limited.status_code == 429


def test_existing_database_gets_read_at_migration(tmp_path: Path) -> None:
    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute("ALTER TABLE documents DROP COLUMN read_at")
    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(documents)")}
    assert {"read_at", "last_page", "last_opened_at"} <= columns


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


def test_legacy_card_schema_migration_preserves_data_and_note_link_fk(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    identify(app.test_client(), "migration-user")
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
        database.execute(
            """INSERT INTO documents
               (id, user_id, kind, name, stored_path, page_count, size, created_at)
               VALUES ('doc', ?, 'pdf', 'paper.pdf', '/tmp/paper.pdf', 1, 1, '2026-01-01')""",
            (user_id,),
        )
        database.execute(
            """INSERT INTO cards
               (id, user_id, document_id, target, target_normalized, sentence, page,
                translations_json, replacement, alternatives_json, created_at)
               VALUES ('legacy-card', ?, 'doc', 'robust', 'robust', 'A robust test.', 1,
                       '["надёжный"]', 'надёжный', '["strong"]', '2026-01-01')""",
            (user_id,),
        )
        database.commit()
        database.execute("PRAGMA foreign_keys = OFF")
        database.executescript(
            """
            DROP TABLE card_highlights;
            DROP TABLE anki_note_links;
            ALTER TABLE cards RENAME TO cards_current;
            CREATE TABLE cards (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                target TEXT NOT NULL,
                target_normalized TEXT NOT NULL,
                sentence TEXT NOT NULL,
                page INTEGER NOT NULL,
                translations_json TEXT NOT NULL,
                replacement TEXT NOT NULL,
                alternatives_json TEXT NOT NULL,
                csv_exported_at TEXT,
                apkg_exported_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, target_normalized)
            );
            INSERT INTO cards
            SELECT id, user_id, document_id, target, target_normalized, sentence, page,
                   translations_json, replacement, alternatives_json, csv_exported_at,
                   apkg_exported_at, created_at FROM cards_current;
            DROP TABLE cards_current;
            CREATE TABLE card_highlights (
                card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                highlight_id TEXT NOT NULL REFERENCES highlights(id) ON DELETE CASCADE,
                PRIMARY KEY(card_id, highlight_id)
            );
            """
        )
        database.commit()

    make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute(
            "SELECT target, sentence FROM cards WHERE id = 'legacy-card'"
        ).fetchone() == ("robust", "A robust test.")
        link_targets = {
            row[2] for row in database.execute("PRAGMA foreign_key_list(anki_note_links)")
        }
        assert "cards" in link_targets


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

    reader = client.get(f"/article/{document['id']}")
    deleted = client.delete(
        f"/api/article/{document['id']}/highlights/{highlight['id']}",
        headers={"X-CSRF-Token": csrf(reader)},
    )
    assert deleted.status_code == 200
    webapp_module.process_document_highlights(app, document["id"], document["user_id"])
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM deleted_highlights").fetchone()[0] == 1


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


def test_pdf_import_does_not_duplicate_overlapping_reader_highlight(
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
        rects=[{"x1": 82, "y1": 191, "x2": 119, "y2": 204}],
    )
    monkeypatch.setattr(webapp_module, "extract_highlights", lambda _path: [extracted])
    app = make_app(tmp_path)
    client = app.test_client()
    dashboard = identify(client)
    client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(dashboard), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
    )
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        document_id, user_id = database.execute(
            "SELECT id, user_id FROM documents"
        ).fetchone()
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

    webapp_module.process_document_highlights(app, document_id, user_id)

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 1
        assert database.execute("SELECT source FROM highlights").fetchone()[0] == "reader"
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM card_highlights").fetchone()[0] == 1


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

    webapp_module.write_pdf_without_native_highlights(source, destination)

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
