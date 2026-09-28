from __future__ import annotations

import pytest
from pydantic import ValidationError

import articles_to_anki.enrichment.targets as ref_enrich
from articles_to_anki.enrich import (
    build_openrouter_payload,
)
from articles_to_anki.models import (
    ClusterAnalysis,
    EnrichedItem,
    EnrichmentRequestItem,
    RecallDistractors,
)


def test_openrouter_payload_uses_strict_pydantic_json_schema() -> None:
    payload = build_openrouter_payload(
        [
            EnrichmentRequestItem(
                id="abc",
                target="retained",
                sentence="The system retained the cached values.",
            )
        ],
        "google/gemma-4-26b-a4b-it",
    )
    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
    item_schema = response_format["json_schema"]["schema"]["$defs"]["EnrichedItem"]
    assert "context_explanation_ru" in item_schema["required"]
    assert "translations_ru" in item_schema["required"]
    assert item_schema["properties"]["translations_ru"]["minItems"] == 1
    assert item_schema["properties"]["forbidden_alternatives_en"]["minItems"] == 0
    assert payload["provider"]["require_parameters"] is True
    assert payload["max_tokens"] == 2500
    assert payload["plugins"] == [{"id": "response-healing"}]


def test_enrichment_rejects_non_russian_replacement_and_duplicates() -> None:
    with pytest.raises(ValidationError):
        EnrichedItem(
            id="abc",
            context_explanation_ru="Здесь речь идёт о сохранении значений.",
            translations_ru=["сохранила", "удержала"],
            replacement_ru="acquisition",
            forbidden_alternatives_en=["purchase", "possession"],
        )
    with pytest.raises(ValidationError):
        EnrichedItem(
            id="abc",
            context_explanation_ru="Здесь действие уменьшает силу эффекта.",
            translations_ru=["смягчить", "ослабить"],
            replacement_ru="смягчить",
            forbidden_alternatives_en=["reduce", "Reduce"],
        )
    with pytest.raises(ValidationError):
        EnrichedItem(
            id="abc",
            context_explanation_ru="Здесь вводное слово выделяет важный результат.",
            translations_ru=["примечательно", "показательно"],
            replacement_ru="Примечательно,",
            forbidden_alternatives_en=["remarkably", "importantly"],
        )
    with pytest.raises(ValidationError):
        EnrichedItem(
            id="abc",
            context_explanation_ru="Здесь речь идёт о сохранении значений.",
            translations_ru=["сохранила", "Сохранила"],
            replacement_ru="сохранила",
            forbidden_alternatives_en=["saved", "kept"],
        )


def test_enrichment_allows_one_precise_translation_and_no_alternatives() -> None:
    item = EnrichedItem(
        id="abc",
        context_explanation_ru="У конструкции есть только один точный перевод.",
        translations_ru=["учёт"],
        replacement_ru="учёт",
        forbidden_alternatives_en=[],
    )
    assert item.translations_ru == ["учёт"]
    assert item.forbidden_alternatives_en == []


def test_cluster_analysis_rejects_loose_pos_and_non_russian_replacements() -> None:
    values = {
        "cluster_id": "new_cluster",
        "leader": "robust",
        "part_of_speech": "adjective",
        "cluster_definition_en": "able to remain reliable under difficult conditions",
        "translations_ru": ["надёжный", "устойчивый"],
        "replacement_ru": "надёжным",
        "source_distractors": {
            "substitutes_en": [],
            "related_en": [],
            "valid_substitutes_en": [],
            "valid_related_en": [],
        },
    }
    assert ClusterAnalysis(**values).part_of_speech == "adjective"
    assert (
        ClusterAnalysis(**{**values, "part_of_speech": "preposition"}).part_of_speech
        == "preposition"
    )
    assert ClusterAnalysis(**{**values, "leader": " Robust "}).leader == "robust"
    with pytest.raises(ValidationError):
        ClusterAnalysis(**{**values, "part_of_speech": "adj"})
    with pytest.raises(ValidationError):
        ClusterAnalysis(**{**values, "replacement_ru": "reliable"})
    with pytest.raises(ValidationError):
        ClusterAnalysis(
            **{
                **values,
                "replacement_ru": (
                    "Оценка оставалась устойчивой даже при очень сильном внешнем шуме"
                ),
            }
        )


def test_recall_distractor_lists_allow_empty_values_and_cross_category_overlap() -> None:
    assert RecallDistractors(
        substitutes_en=[],
        related_en=[],
        valid_substitutes_en=[],
        valid_related_en=[],
    ).valid_substitutes_en == []
    overlapping = RecallDistractors(
        substitutes_en=["account"],
        related_en=["Account"],
        valid_substitutes_en=["account"],
        valid_related_en=["Account"],
    )
    assert overlapping.valid_related_en == ["Account"]
    with pytest.raises(ValidationError):
        RecallDistractors(
            substitutes_en=["account", "Account"],
            related_en=[],
            valid_substitutes_en=[],
            valid_related_en=[],
        )


def test_recall_distractors_must_not_repeat_target() -> None:
    distractors = RecallDistractors(
        substitutes_en=["account"],
        related_en=["thought"],
        valid_substitutes_en=["account"],
        valid_related_en=["thought"],
    )
    ref_enrich._validate_recall_distractors("consideration", distractors)
    ref_enrich._validate_recall_distractors(
        "take into account",
        RecallDistractors(
            substitutes_en=[" Take  into   Account "],
            related_en=[],
            valid_substitutes_en=[],
            valid_related_en=[],
        ),
    )
    with pytest.raises(RuntimeError, match="must not contain the target"):
        ref_enrich._validate_recall_distractors(
            "take into account",
            RecallDistractors(
                substitutes_en=[],
                related_en=[],
                valid_substitutes_en=[" Take  into   Account "],
                valid_related_en=[],
            ),
        )


def test_cluster_analysis_schema_requires_cluster_choice_and_leader() -> None:
    schema = ClusterAnalysis.model_json_schema()
    assert {"cluster_id", "leader", "cluster_definition_en"} <= set(schema["required"])
