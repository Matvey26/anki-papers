from __future__ import annotations

import io
import re
from pathlib import Path

from pypdf import PdfWriter

import articles_to_anki.cards.llm_cache as ref_cards_llm_cache
import articles_to_anki.enrich as ref_enrich
from articles_to_anki.models import ClusterAnalysis, EnrichedItem
from articles_to_anki.webapp import create_app


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

    monkeypatch.setattr(ref_enrich, "enrich_targets", fake_enrich)

    def fake_cluster(target, normalized_target, sentence, candidates, **_kwargs):
        assert target == "robust"
        assert normalized_target == "robust"
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

    monkeypatch.setattr(ref_cards_llm_cache, "analyse_cluster_assignment", fake_cluster)
