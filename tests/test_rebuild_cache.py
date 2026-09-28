from __future__ import annotations

import sqlite3
from pathlib import Path

from support_rebuild import (
    _add_highlight,
)
from support_webapp import csrf, identify, install_fake_enrichment, make_app, pdf_bytes

import articles_to_anki.cards.llm_cache as ref_cards_llm_cache
import articles_to_anki.models as ref_models
from articles_to_anki.models import ClusterAnalysis, ClusterCandidate, RecallDistractors


def test_rebuild_reuses_llm_cache_after_card_removal(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    original = ref_cards_llm_cache.analyse_cluster_assignment
    calls: list[tuple] = []

    def counting_cluster(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(ref_cards_llm_cache, "analyse_cluster_assignment", counting_cluster)
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
    assert len(calls) == 1

    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        database.execute("DELETE FROM card_highlights")
        database.execute("DELETE FROM cards")
        database.commit()

    response = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(client.get("/settings"))},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert len(calls) == 1

    second_response = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(client.get("/settings"))},
        content_type="multipart/form-data",
    )
    assert second_response.status_code == 302
    assert len(calls) == 1


def test_cluster_analysis_cached_skips_repeated_calls(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    calls = {"count": 0}

    def fake(target, normalized_target, sentence, candidates, **kwargs):
        calls["count"] += 1
        return ClusterAnalysis(
            cluster_id="new_cluster",
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

    monkeypatch.setattr(ref_cards_llm_cache, "analyse_cluster_assignment", fake)
    cache_dir = tmp_path / "caches"
    kwargs = {
        "model": "model-x",
        "target": "robust",
        "normalized_target": "robust",
        "sentence": "The design proved robust in tests.",
        "candidates": [],
        "api_key": "key",
    }
    analysis, was_cached = ref_cards_llm_cache.cluster_analysis_cached(
        cache_dir, 7, **kwargs
    )
    assert not was_cached
    assert analysis.leader == "robust"
    second, was_cached = ref_cards_llm_cache.cluster_analysis_cached(cache_dir, 7, **kwargs)
    assert was_cached
    assert second == analysis
    assert calls["count"] == 1


def test_context_approval_cached_skips_repeated_calls(tmp_path: Path, monkeypatch) -> None:
    calls = {"count": 0}

    def fake(leader, definition, translations, known_contexts, candidates, **kwargs):
        calls["count"] += 1
        assert leader == "robust"
        return {item.id for item in candidates}

    monkeypatch.setattr(ref_cards_llm_cache, "approve_context_candidates", fake)
    cache_dir = tmp_path / "caches"
    kwargs = {
        "model": "model-x",
        "leader": "robust",
        "definition": "definition",
        "translations": ["надёжный"],
        "known_contexts": ["known sentence"],
        "candidates": [
            ref_models.ContextCandidate(id="a", surface="robust", sentence="One sentence."),
            ref_models.ContextCandidate(id="b", surface="robust", sentence="Another sentence."),
        ],
        "api_key": "key",
    }
    first = ref_cards_llm_cache.context_approval_cached(cache_dir, 7, **kwargs)
    second = ref_cards_llm_cache.context_approval_cached(cache_dir, 7, **kwargs)
    assert first == {"a", "b"}
    assert second == {"a", "b"}
    assert calls["count"] == 1


def test_cluster_analysis_cached_coerces_dict_candidates(tmp_path: Path, monkeypatch) -> None:
    install_fake_enrichment(monkeypatch)
    calls: dict[str, int] = {"count": 0}

    def collecting_cluster(*args, **kwargs):
        calls["count"] += 1
        candidates = kwargs.get("candidates") if "candidates" in kwargs else args[3]
        assert all(isinstance(candidate, ClusterCandidate) for candidate in candidates)
        return ClusterAnalysis(
            cluster_id=str(candidates[0].cluster_id),
            leader=str(candidates[0].leader),
            part_of_speech="adjective",
            cluster_definition_en="definition",
            translations_ru=["надёжный"],
            replacement_ru="надёжный",
            source_distractors=RecallDistractors(
                substitutes_en=[],
                related_en=[],
                valid_substitutes_en=[],
                valid_related_en=[],
            ),
        )

    monkeypatch.setattr(ref_cards_llm_cache, "analyse_cluster_assignment", collecting_cluster)
    kwargs = {
        "cache_dir": tmp_path / "caches",
        "user_id": 7,
        "model": "model",
        "target": "robust",
        "normalized_target": "robust",
        "sentence": "A robust result.",
        "candidates": [
            {"cluster_id": "c1", "leader": "robust", "examples": [
                {"highlight": "robust", "context": "A robust result."}
            ]}
        ],
        "api_key": "key",
    }
    first, first_cached = ref_cards_llm_cache.cluster_analysis_cached(**kwargs)
    second, second_cached = ref_cards_llm_cache.cluster_analysis_cached(**kwargs)
    assert first.cluster_id == "c1" and not first_cached
    assert second.cluster_id == "c1" and second_cached
    assert calls["count"] == 1
