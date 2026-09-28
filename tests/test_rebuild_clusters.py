from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from support_rebuild import (
    _read_rebuilt,
    _start_rebuild,
)
from support_rebuild_fixtures import _rebuild_download
from support_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.cards.llm_cache as ref_cards_llm_cache
import articles_to_anki.rebuilding.seeds as ref_rebuilding_seeds
import articles_to_anki.rebuilding.types as ref_rebuilding_types
from articles_to_anki.models import ClusterAnalysis, ClusterCandidate


def test_rebuild_merges_clusters_with_colliding_ids(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)

    def constant_leader(target, normalized_target, sentence, candidates, **_kwargs):
        return ClusterAnalysis(
            cluster_id=candidates[0].cluster_id if candidates else "new_cluster",
            leader="robust",
            part_of_speech="adjective",
            cluster_definition_en="reliable and resilient in operation",
            translations_ru=["надёжный", "устойчивый"],
            replacement_ru="надёжный",
            source_distractors={
                "substitutes_en": ["strong", "durable"],
                "related_en": ["strength", "resilience"],
                "valid_substitutes_en": ["strong"],
                "valid_related_en": ["strength"],
            },
        )

    monkeypatch.setattr(ref_cards_llm_cache, "analyse_cluster_assignment", constant_leader)
    app = make_app(tmp_path, REBUILD_INLINE=True)
    client = app.test_client()
    response = identify(client)

    response = client.post(
        "/upload/pdf",
        data={"csrf_token": csrf(response), "file": (pdf_bytes(), "paper.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    sentence = "This is a robust result."
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
        document_id = database.execute(
            "SELECT id FROM documents WHERE kind = 'pdf' ORDER BY created_at LIMIT 1"
        ).fetchone()[0]
        for index, target in enumerate(("robust", "qqq")):
            database.execute(
                """INSERT INTO highlights
                   (id, user_id, document_id, target, sentence, page, rects_json,
                    translations_json, replacement, alternatives_json, status,
                    error, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '', '[]', 'ready', NULL,
                           'reader', ?, ?)""",
                (
                    str(uuid.uuid4()),
                    user_id,
                    document_id,
                    target,
                    sentence,
                    1,
                    f"[{index}]",
                    f"2026-08-18T1{index}:00:00",
                    f"2026-08-18T1{index}:00:00",
                ),
            )
        database.commit()

    _start_rebuild(client, tmp_path)
    _payload, download = _rebuild_download(client)
    database = _read_rebuilt(tmp_path, download)
    try:
        note_ids = [row[0] for row in database.execute("SELECT id FROM notes")]
        assert len(note_ids) == len(set(note_ids)) == 2
    finally:
        database.close()


def test_cluster_candidate_models_builds_valid_objects() -> None:
    clusters = [
        ref_rebuilding_types.RebuildCluster(
            id="c1",
            target="robust",
            leader="robust",
            sentence="A robust result.",
            part_of_speech="adjective",
            sense_definition_en="definition",
            translations=["надёжный"],
            contexts=[
                {"target": "Robust", "sentence": "  A robust result.  "},
                {"target": "", "sentence": "No target here."},
            ],
            highlight_ids=["h1", "h2"],
            old_card_ids=[],
            source_page=1,
            document_id="a",
        ),
        ref_rebuilding_types.RebuildCluster(
            id="c2",
            target="robust",
            leader="",
            sentence="Orphan cluster.",
            part_of_speech="adjective",
            sense_definition_en="",
            translations=[],
            contexts=[{"target": "robust", "sentence": "Orphan context."}],
            highlight_ids=["h3"],
            old_card_ids=[],
            source_page=1,
            document_id="b",
        ),
        ref_rebuilding_types.RebuildCluster(
            id="c3",
            target="robust",
            leader="orphan",
            sentence="Empty contexts.",
            part_of_speech="adjective",
            sense_definition_en="",
            translations=[],
            contexts=[{"target": "", "sentence": ""}],
            highlight_ids=["h4"],
            old_card_ids=[],
            source_page=1,
            document_id="c",
        ),
    ]
    candidates = ref_rebuilding_seeds._cluster_candidate_models(clusters)
    assert [candidate.cluster_id for candidate in candidates] == ["c1"]
    candidate = candidates[0]
    assert isinstance(candidate, ClusterCandidate)
    assert candidate.leader == "robust"
    assert [example.highlight for example in candidate.examples] == ["robust"]
    assert candidate.examples[0].context == "A robust result."


def test_collect_seeds_accepts_all_highlight_sources(tmp_path: Path) -> None:
    database = sqlite3.connect(tmp_path / "seeds.sqlite")
    try:
        database.executescript(
            """
            CREATE TABLE highlights (id TEXT PRIMARY KEY, user_id INTEGER);
            CREATE TABLE cards (
                id TEXT PRIMARY KEY, user_id INTEGER, semantic_version INTEGER,
                contexts_json TEXT
            );
            CREATE TABLE card_highlights (
                card_id TEXT, highlight_id TEXT, PRIMARY KEY(card_id, highlight_id)
            );
            """
        )
        database.execute("INSERT INTO highlights VALUES ('h1', 1), ('h2', 1), ('other', 1)")
        context = {
            "id": "h1",
            "source": "reader",
            "target": "robust",
            "sentence": "A robust result.",
            "lemma": "robust",
            "part_of_speech": "adjective",
            "sense_definition_en": "definition",
            "translations": ["надёжный"],
            "replacement": "надёжный",
            "substitutes_en": [],
            "related_en": [],
            "valid_substitutes_en": [],
            "valid_related_en": [],
        }
        pdf_context = {
            "id": "h2",
            "source": "pdf_import",
            "target": "result",
            "sentence": "A final result.",
            "lemma": "result",
            "part_of_speech": "noun",
            "sense_definition_en": "outcome",
            "translations": ["итог"],
            "replacement": "итог",
            "substitutes_en": [],
            "related_en": [],
            "valid_substitutes_en": [],
            "valid_related_en": [],
        }
        article_context = {
            "id": "article:1:ctx-0001",
            "source": "article_context",
            "target": "robust",
            "sentence": "A robust result.",
            "lemma": "robust",
            "part_of_speech": "adjective",
            "sense_definition_en": "definition",
            "translations": ["надёжный"],
            "replacement": "надёжный",
            "substitutes_en": [],
            "related_en": [],
            "valid_substitutes_en": [],
            "valid_related_en": [],
        }
        database.execute(
            "INSERT INTO card_highlights VALUES ('card1', 'h1'), ('card1', 'h2')"
        )
        database.execute(
            "INSERT INTO cards VALUES ('card1', 1, 1, ?)",
            (json.dumps([context, pdf_context, article_context]),),
        )
        database.execute("INSERT INTO cards VALUES ('card2', 1, 1, ?)", ("[]",))
        database.execute("INSERT INTO cards VALUES ('card3', 1, 0, ?)", ("[]",))
        database.row_factory = sqlite3.Row
        seeds, seeds_old_cards = ref_rebuilding_seeds._collect_seeds(database, 1)
    finally:
        database.close()
    assert set(seeds) == {"h1", "h2"}
    assert seeds["h1"]["cluster_id"] == "new_cluster"
    assert seeds["h1"]["leader"] == "robust"
    assert seeds_old_cards == {"h1": "card1", "h2": "card1"}
