from __future__ import annotations

import csv

import pytest
from pydantic import ValidationError

import articles_to_anki.enrich as enrich_module
from articles_to_anki.cli import _exclude_processed_targets, _load_excluded_targets
from articles_to_anki.enrich import (
    analyse_semantic_context,
    build_openrouter_payload,
    enrich_targets,
    select_semantic_match,
)
from articles_to_anki.export import write_anki_csv
from articles_to_anki.extract import (
    RECALL_PLACEHOLDER,
    ExtractionConfig,
    Token,
    _group_selected_tokens,
    _pdf_quad_to_region,
    _rectangle_overlap_ratio,
    is_sentence_end,
    render_sentence,
)
from articles_to_anki.models import (
    EnrichedItem,
    EnrichmentRequestItem,
    RecallDistractors,
    SemanticAnalysis,
    SemanticCandidate,
    SemanticMatchResponse,
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
    assert item_schema["properties"]["translations_ru"]["minItems"] == 1
    assert item_schema["properties"]["forbidden_alternatives_en"]["minItems"] == 0
    assert payload["provider"]["require_parameters"] is True
    assert payload["max_tokens"] == 2500
    assert payload["plugins"] == [{"id": "response-healing"}]


def test_default_deepseek_payload_disables_reasoning() -> None:
    assert enrich_module.DEFAULT_MODEL == "deepseek/deepseek-v4-flash-0731:nitro"
    assert enrich_module.DEFAULT_SEMANTIC_MODEL == enrich_module.DEFAULT_MODEL
    payload = build_openrouter_payload(
        [
            EnrichmentRequestItem(
                id="abc",
                target="retained",
                sentence="The system retained the cached values.",
            )
        ],
        enrich_module.DEFAULT_MODEL,
    )
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["temperature"] == 0.2
    assert payload["provider"]["require_parameters"] is True


def test_luna_payload_omits_unsupported_temperature() -> None:
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


def test_semantic_deepseek_payloads_disable_reasoning(monkeypatch) -> None:
    payloads = []
    analysis = SemanticAnalysis(
        lemma="acknowledge",
        family_key="acknowledge",
        part_of_speech="verb",
        sense_definition_en="accept that a fact or limitation is true",
        translations_ru=["признать"],
        replacement_ru="признала",
        generated_sentence="The committee acknowledged the limitation before proceeding.",
        generated_surface="acknowledged",
        generated_translation_ru="признал",
        source_distractors={
            "valid_substitutes_en": ["accepted"],
            "related_but_uninsertable_en": ["acceptance"],
        },
        generated_distractors={
            "valid_substitutes_en": ["recognized"],
            "related_but_uninsertable_en": ["recognition"],
        },
    )

    def fake_request(payload, _api_key):
        payloads.append(payload)
        if payload["response_format"]["json_schema"]["name"] == "semantic_context_analysis":
            content = analysis.model_dump_json()
        else:
            content = SemanticMatchResponse(
                match={
                    "card_id": "card-1",
                    "relationship": "same_sense",
                    "merged_sense_definition_en": analysis.sense_definition_en,
                    "rationale_ru": "Значение и управление совпадают.",
                }
            ).model_dump_json()
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(enrich_module, "_post_json", fake_request)
    result = analyse_semantic_context(
        "acknowledged",
        "The committee acknowledged the limitation.",
        api_key="test-key",
    )
    select_semantic_match(
        result,
        [
            SemanticCandidate(
                id="card-1",
                family_key="acknowledge",
                lemmas=["acknowledge"],
                parts_of_speech=["verb"],
                sense_definition_en=analysis.sense_definition_en,
            )
        ],
        api_key="test-key",
    )

    assert [payload["model"] for payload in payloads] == [
        enrich_module.DEFAULT_SEMANTIC_MODEL,
        enrich_module.DEFAULT_SEMANTIC_MODEL,
    ]
    assert [payload["reasoning"] for payload in payloads] == [
        {"effort": "none"},
        {"effort": "none"},
    ]
    assert [payload["temperature"] for payload in payloads] == [0.2, 0.0]
    assert all(payload["provider"]["require_parameters"] for payload in payloads)


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


def test_semantic_analysis_rejects_loose_pos_and_non_russian_replacements() -> None:
    values = {
        "lemma": "robust",
        "family_key": "robust",
        "part_of_speech": "adjective",
        "sense_definition_en": "able to remain reliable under difficult conditions",
        "translations_ru": ["надёжный", "устойчивый"],
        "replacement_ru": "надёжным",
        "generated_sentence": "The estimator remained robust under heavy noise.",
        "generated_surface": "robust",
        "generated_translation_ru": "устойчивым",
        "source_distractors": {
            "valid_substitutes_en": [],
            "related_but_uninsertable_en": [],
        },
        "generated_distractors": {
            "valid_substitutes_en": [],
            "related_but_uninsertable_en": [],
        },
    }
    assert SemanticAnalysis(**values).part_of_speech == "adjective"
    assert SemanticAnalysis(**{**values, "family_key": " Robust "}).family_key == "robust"
    with pytest.raises(ValidationError):
        SemanticAnalysis(**{**values, "part_of_speech": "adj"})
    with pytest.raises(ValidationError):
        SemanticAnalysis(**{**values, "replacement_ru": "reliable"})
    with pytest.raises(ValidationError):
        SemanticAnalysis(**{**values, "generated_translation_ru": "stable"})
    with pytest.raises(ValidationError):
        SemanticAnalysis(
            **{
                **values,
                "generated_translation_ru": (
                    "Оценка оставалась устойчивой даже при очень сильном внешнем шуме"
                ),
            }
        )


def test_recall_distractor_categories_are_disjoint_and_allow_empty_lists() -> None:
    assert RecallDistractors(
        valid_substitutes_en=[],
        related_but_uninsertable_en=[],
    ).valid_substitutes_en == []
    with pytest.raises(ValidationError):
        RecallDistractors(
            valid_substitutes_en=["account"],
            related_but_uninsertable_en=["Account"],
        )


def test_recall_distractors_must_not_repeat_target() -> None:
    distractors = RecallDistractors(
        valid_substitutes_en=["account"],
        related_but_uninsertable_en=["thought"],
    )
    enrich_module._validate_recall_distractors("consideration", distractors)
    with pytest.raises(RuntimeError, match="must not contain the target"):
        enrich_module._validate_recall_distractors(
            "take into account",
            RecallDistractors(
                valid_substitutes_en=[" Take  into   Account "],
                related_but_uninsertable_en=[],
            ),
        )


def test_semantic_json_code_fence_is_removed_without_changing_json() -> None:
    value = '{"lemma":"robust"}'
    assert enrich_module._strip_json_code_fence(value) == value
    assert enrich_module._strip_json_code_fence(f"```json\n{value}\n```") == value


def test_generated_surface_must_be_a_complete_word_or_phrase() -> None:
    assert enrich_module._contains_exact_surface(
        "The estimator remained robust under heavy noise.",
        "robust",
    )
    assert enrich_module._contains_exact_surface(
        "The team ruled out a measurement error.",
        "ruled out",
    )
    assert not enrich_module._contains_exact_surface(
        "The estimator's robustness improved.",
        "robust",
    )


def test_semantic_match_schema_requires_nullable_card_id() -> None:
    schema = SemanticMatchResponse.model_json_schema()
    match_schema = schema["$defs"]["SemanticMatch"]
    assert {
        "card_id",
        "relationship",
        "merged_sense_definition_en",
        "rationale_ru",
    } <= set(match_schema["required"])
    assert {item["type"] for item in match_schema["properties"]["card_id"]["anyOf"]} == {
        "string",
        "null",
    }


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


def test_export_omits_empty_forbidden_hint(tmp_path) -> None:
    target = TargetContext(
        id="id-1",
        target="consideration",
        sentence="We take balance into consideration.",
        sentence_html="We take balance into <b>consideration</b>.",
        recall_template_html=f"We take balance into {RECALL_PLACEHOLDER}.",
        source_page=1,
        highlight_coverage=1,
    )
    enrichment = EnrichedItem(
        id="id-1",
        context_explanation_ru="Здесь конструкция означает учёт фактора.",
        translations_ru=["учёт"],
        replacement_ru="учёт",
        forbidden_alternatives_en=[],
    )
    destination = tmp_path / "cards.csv"

    write_anki_csv(destination, [target], [enrichment], article_tag="test")

    with destination.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    recall = next(row for row in rows if "card::recall" in row["Tags"])
    assert "Нельзя использовать" not in recall["Front"]


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
