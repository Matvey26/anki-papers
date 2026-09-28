from __future__ import annotations

import csv
import io
import json
import sqlite3
import uuid
from pathlib import Path

from support_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.cards.clusters as ref_cards_clusters
import articles_to_anki.cards.llm_cache as ref_cards_llm_cache
import articles_to_anki.cards.rendering as ref_cards_rendering
import articles_to_anki.cards.text as ref_cards_text
from articles_to_anki.models import ClusterAnalysis


def test_highlight_normalization_removes_noise_but_preserves_word_punctuation() -> None:
    assert ref_cards_text.normalize_selected_text(
        '  “IT’S\n worth!!!🦆  '
    ) == "it's worth"
    assert ref_cards_text.normalize_selected_text("cutting-edge") == "cutting-edge"
    assert ref_cards_text.normalize_selected_text("attrib-\nuted") == "attributed"
    assert ref_cards_text.normalize_selected_text("(Train),") == "train"


def test_fuzzy_cluster_lookup_uses_leaders_keeps_homonyms_and_caps_payload(
    tmp_path: Path,
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
        database.row_factory = sqlite3.Row
        document_id = database.execute("SELECT id FROM documents").fetchone()[0]
        for index in range(6):
            contexts = [
                {
                    "id": f"highlight-{index}-{example}",
                    "source": "user_pdf",
                    "target": "train",
                    "sentence": f"Distinct train context {index}-{example}.",
                }
                for example in range(6)
            ]
            database.execute(
                """INSERT INTO cards
                   (id, user_id, document_id, target, target_normalized, sentence,
                    page, translations_json, replacement, alternatives_json,
                    lemma, family_key, part_of_speech, sense_definition_en,
                    contexts_json, semantic_version, created_at)
                   VALUES (?, 1, ?, 'train', 'train', ?, 1, '[]', '', '[]',
                           'train', 'train', 'other', ?, ?, 1, ?)""",
                (
                    f"cluster-{index}",
                    document_id,
                    f"Seed sentence {index}.",
                    f"Separate homonymous sense {index}.",
                    json.dumps(contexts),
                    f"2026-08-16T00:00:0{index}+00:00",
                ),
            )
        database.commit()

        candidates = ref_cards_clusters.find_cluster_candidates(
            database, 1, "training", limit=99
        )
        indexes = {
            row[1] for row in database.execute("PRAGMA index_list(cards)")
        }

    assert len(candidates) == 5
    assert len({candidate.cluster_id for candidate in candidates}) == 5
    assert {candidate.leader for candidate in candidates} == {"train"}
    assert all(len(candidate.examples) == 5 for candidate in candidates)
    assert "idx_cards_cluster_leader" in indexes
    assert "idx_cards_semantic_family_lookup" not in indexes


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
        assert len(contexts) == 2
        assert contexts[0]["substitutes_en"] == ["strong", "durable"]
        assert contexts[0]["related_en"] == ["strength", "resilience"]
        assert contexts[0]["valid_substitutes_en"] == ["strong"]
        assert contexts[0]["valid_related_en"] == ["strength"]
        full_card = database.execute(
            "SELECT * FROM cards WHERE target_normalized = 'robust'"
        ).fetchone()
    rows = list(
        csv.DictReader(
            io.StringIO(ref_cards_rendering.cards_to_csv([full_card]).decode("utf-8-sig"))
        )
    )
    assert "Подходит, но не целевой ответ: strong" in rows[1]["Front"]
    assert "Близко по смыслу: strength" in rows[1]["Front"]
    assert "durable" not in rows[1]["Front"]
    assert "resilience" not in rows[1]["Front"]

    restarted = make_app(tmp_path)
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM card_highlights").fetchone()[0] == 2
    restarted.extensions["highlight_executor"].shutdown(
        wait=False, cancel_futures=True
    )


def test_lexical_family_merges_derivations_but_splits_polysemy(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    analyses = {
        "acquired": ClusterAnalysis(
            cluster_id="new_cluster",
            leader="acquire",
            part_of_speech="verb",
            cluster_definition_en="obtain or gain possession of something",
            translations_ru=["приобрести", "получить"],
            replacement_ru="приобрела",
            source_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
        ),
        "acquisition": ClusterAnalysis(
            cluster_id="new_cluster",
            leader="acquire",
            part_of_speech="noun",
            cluster_definition_en="the act or process of obtaining something",
            translations_ru=["приобретение", "получение"],
            replacement_ru="получение",
            source_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
        ),
        "recognize-identify": ClusterAnalysis(
            cluster_id="new_cluster",
            leader="recognize",
            part_of_speech="verb",
            cluster_definition_en="identify something from previous knowledge",
            translations_ru=["распознать", "узнать"],
            replacement_ru="распознать",
            source_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
        ),
        "recognize-admit": ClusterAnalysis(
            cluster_id="new_cluster",
            leader="recognize",
            part_of_speech="verb",
            cluster_definition_en="admit or acknowledge that something is true",
            translations_ru=["признать", "признавать"],
            replacement_ru="признать",
            source_distractors={"substitutes_en": [], "related_en": [], "valid_substitutes_en": [], "valid_related_en": []},
        ),
    }

    def fake_analysis(target, _normalized, sentence, candidates, **_kwargs):
        if target == "acquisition":
            assert candidates[0].leader == "acquire"
            return analyses[target].model_copy(
                update={
                    "cluster_id": candidates[0].cluster_id,
                    "cluster_definition_en": (
                        "obtain something or the process of obtaining it"
                    ),
                }
            )
        if target != "recognize":
            return analyses[target]
        key = "recognize-identify" if "face" in sentence else "recognize-admit"
        return analyses[key]
    monkeypatch.setattr(ref_cards_llm_cache, "analyse_cluster_assignment", fake_analysis)
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
    assert {context["target"] for context in acquire_contexts} >= {
        "acquired",
        "acquisition",
    }
    assert {context["lemma"] for context in acquire_contexts} == {"acquire"}
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
                ref_cards_rendering.cards_to_csv(acquire_cards).decode("utf-8-sig")
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
