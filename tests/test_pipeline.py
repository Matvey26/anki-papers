from __future__ import annotations

import csv

import articles_to_anki.enrich as enrich_module
from articles_to_anki.cli import _exclude_processed_targets, _load_excluded_targets
from articles_to_anki.enrich import build_openrouter_payload, enrich_targets
from articles_to_anki.export import write_anki_csv
from articles_to_anki.extract import (
    ExtractionConfig,
    RECALL_PLACEHOLDER,
    Token,
    _group_selected_tokens,
    _pdf_quad_to_region,
    _rectangle_overlap_ratio,
    is_sentence_end,
    render_sentence,
)
import pytest
from pydantic import ValidationError

from articles_to_anki.models import (
    EnrichedItem,
    EnrichmentRequestItem,
    TargetContext,
)


def token(text: str, index: int, *, x0: float | None = None) -> Token:
    left = float(index * 20 if x0 is None else x0)
    return Token(
        text=text,
        page_index=0,
        x0=left,
        x1=left + 15,
        top=100,
        bottom=111,
        local_index=index,
        global_index=index,
        selected=True,
        coverage=0.8,
    )


def test_adjacent_highlighted_words_form_phrase() -> None:
    tokens = [
        token("By", 0, x0=70),
        token("and", 1, x0=88),
        token("large,", 2, x0=110),
        token("DeepSeek", 3, x0=150),
    ]
    tokens[-1].selected = False
    groups = _group_selected_tokens(tokens, config=ExtractionConfig())
    assert len(groups) == 1
    assert groups[0].target == "By and large"


def test_phrase_can_cross_inline_math_and_a_line_wrap() -> None:
    tokens = [
        token("and", 0, x0=399),
        token("𝑊𝑈𝑉", 1, x0=421),
        token("can", 2, x0=444),
        token("be", 3, x0=465),
        token("absorbed", 4, x0=479),
        token("into", 5, x0=71),
    ]
    tokens[1].selected = False
    tokens[1].top = 96
    tokens[1].bottom = 107
    tokens[-1].top = 113
    tokens[-1].bottom = 124
    groups = _group_selected_tokens(tokens, config=ExtractionConfig())
    assert len(groups) == 1
    assert groups[0].target == "can be absorbed into"


def test_target_rendering_bolds_only_target_and_keeps_punctuation() -> None:
    tokens = [token("It", 0), token("comprises", 1), token("236B", 2), token("parameters.", 3)]
    assert render_sentence(tokens, target_range=(1, 1)) == (
        "It <b>comprises</b> 236B parameters."
    )
    assert render_sentence(
        tokens,
        target_range=(1, 1),
        replacement=RECALL_PLACEHOLDER,
    ) == f"It {RECALL_PLACEHOLDER} 236B parameters."


def test_line_wrap_hyphen_is_removed() -> None:
    tokens = [token("demon-", 0), token("strate", 1), token("efficiency.", 2)]
    assert render_sentence(tokens) == "demonstrate efficiency."


def test_sentence_end_ignores_common_abbreviations() -> None:
    assert not is_sentence_end("al.")
    assert not is_sentence_end("Fig.")
    assert is_sentence_end("(AGI).")
    assert is_sentence_end("models.")


def test_pdf_highlight_quad_uses_top_origin_coordinates() -> None:
    region = _pdf_quad_to_region(
        [10, 90, 50, 90, 10, 80, 50, 80],
        crop_left=0,
        crop_bottom=0,
        page_height=100,
    )
    assert region == (10, 10, 50, 20)
    assert _rectangle_overlap_ratio((20, 10, 60, 20), region) == 0.75


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
    assert item_schema["properties"]["translations_ru"]["minItems"] == 2
    assert payload["provider"]["require_parameters"] is True
    assert payload["max_tokens"] == 2500
    assert payload["plugins"] == [{"id": "response-healing"}]


def test_luna_payload_omits_unsupported_temperature() -> None:
    assert enrich_module.DEFAULT_MODEL == "openai/gpt-5.6-luna"
    payload = build_openrouter_payload(
        [
            EnrichmentRequestItem(
                id="abc",
                target="retained",
                sentence="The system retained the cached values.",
            )
        ],
        "openai/gpt-5.6-luna",
    )
    assert "temperature" not in payload
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["provider"]["require_parameters"] is True


def test_enrichment_retries_five_times_with_varied_requests(monkeypatch) -> None:
    payloads = []

    def fail_request(payload, _api_key):
        payloads.append(payload)
        raise OSError("provider unavailable")

    monkeypatch.setattr(enrich_module, "_post_json", fail_request)
    monkeypatch.setattr(enrich_module.time, "sleep", lambda _seconds: None)
    target = TargetContext(
        id="abc",
        target="retained",
        sentence="The system retained the cached values.",
        sentence_html="The system <b>retained</b> the cached values.",
        recall_template_html=f"The system {RECALL_PLACEHOLDER} the cached values.",
        source_page=1,
        highlight_coverage=0.8,
    )

    with pytest.raises(RuntimeError, match="after 5 attempts"):
        enrich_targets(
            [target],
            api_key="test-key",
            model="google/gemma-4-26b-a4b-it",
        )

    assert len(payloads) == 5
    assert [payload["temperature"] for payload in payloads] == [
        0.2,
        0.1,
        0.3,
        0.0,
        0.25,
    ]
    retry_prompts = [payload["messages"][-1]["content"] for payload in payloads[1:]]
    assert len(set(retry_prompts)) == 4


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


def test_csv_cards_are_shuffled_reproducibly(tmp_path) -> None:
    targets = [
        TargetContext(
            id=f"id-{index}",
            target=f"target{index}",
            sentence=f"Sentence with target{index}.",
            sentence_html=f"Sentence with <b>target{index}</b>.",
            recall_template_html=f"Sentence with {RECALL_PLACEHOLDER}.",
            source_page=index + 1,
            highlight_coverage=0.8,
        )
        for index in range(4)
    ]
    enrichments = [
        EnrichedItem(
            id=f"id-{index}",
            context_explanation_ru="Контекст однозначно задаёт это значение.",
            translations_ru=[f"перевод {index}", f"вариант {index}"],
            replacement_ru=f"перевод {index}",
            forbidden_alternatives_en=[f"simple{index}", f"plain{index}"],
        )
        for index in range(4)
    ]
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    third = tmp_path / "third.csv"
    write_anki_csv(first, targets, enrichments, article_tag="test", shuffle_seed=42)
    write_anki_csv(second, targets, enrichments, article_tag="test", shuffle_seed=42)
    write_anki_csv(third, targets, enrichments, article_tag="test", shuffle_seed=7)

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() != third.read_bytes()
    with first.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 8
    meaning_rows = [row for row in rows if "card::meaning" in row["Tags"]]
    assert all("<br>" in row["Back"] for row in meaning_rows)


def test_deduplication_skips_history_but_keeps_repeated_current_targets(tmp_path) -> None:
    history = tmp_path / "extracted_targets.json"
    history.write_text(
        '{"targets": [{"target": " Harness  "}]}', encoding="utf-8"
    )
    targets = [
        TargetContext(
            id=f"id-{index}",
            target=value,
            sentence=f"Sentence with {value}.",
            sentence_html=f"Sentence with <b>{value}</b>.",
            recall_template_html=f"Sentence with {RECALL_PLACEHOLDER}.",
            source_page=1,
            highlight_coverage=0.8,
        )
        for index, value in enumerate(["harness", "Notably", "notably", "surpasses"])
    ]
    kept, skipped = _exclude_processed_targets(
        targets, _load_excluded_targets([history])
    )
    assert [target.target for target in kept] == ["Notably", "notably", "surpasses"]
    assert [target.target for target in skipped] == ["harness"]
