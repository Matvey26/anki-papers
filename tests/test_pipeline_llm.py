from __future__ import annotations

import json

import pytest

import articles_to_anki.cards.llm_cache as ref_cards_llm_cache
import articles_to_anki.enrichment.client as ref_enrichment_client
import articles_to_anki.enrichment.prompts as ref_enrichment_prompts
import articles_to_anki.enrichment.targets as ref_enrich
from articles_to_anki.enrich import (
    analyse_cluster_assignment,
    build_openrouter_payload,
    enrich_targets,
)
from articles_to_anki.extract import (
    RECALL_PLACEHOLDER,
)
from articles_to_anki.models import (
    ClusterAnalysis,
    ClusterCandidate,
    ContextCandidate,
    EnrichmentRequestItem,
    TargetContext,
)


def test_default_deepseek_payload_disables_reasoning() -> None:
    assert ref_enrichment_prompts.DEFAULT_MODEL == "deepseek/deepseek-v4-flash-0731:nitro"
    assert ref_enrichment_prompts.DEFAULT_SEMANTIC_MODEL == ref_enrichment_prompts.DEFAULT_MODEL
    payload = build_openrouter_payload(
        [
            EnrichmentRequestItem(
                id="abc",
                target="retained",
                sentence="The system retained the cached values.",
            )
        ],
        ref_enrichment_prompts.DEFAULT_MODEL,
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


def test_cluster_assignment_uses_reasoning_and_dynamic_cluster_enum(monkeypatch) -> None:
    payloads = []
    analysis = ClusterAnalysis(
        cluster_id="cluster-1",
        leader="acknowledge",
        part_of_speech="verb",
        cluster_definition_en="accept that a fact or limitation is true",
        translations_ru=["признать"],
        replacement_ru="признала",
        source_distractors={
            "substitutes_en": ["accepted"],
            "related_en": ["acceptance"],
            "valid_substitutes_en": ["accepted"],
            "valid_related_en": ["acceptance"],
        },
    )

    def fake_request(payload, _api_key):
        payloads.append(payload)
        return {"choices": [{"message": {"content": analysis.model_dump_json()}}]}

    monkeypatch.setattr(ref_enrichment_client, "_post_json", fake_request)
    result = analyse_cluster_assignment(
        "acknowledged",
        "acknowledged",
        "The committee acknowledged the limitation.",
        [
            ClusterCandidate(
                cluster_id="cluster-1",
                leader="acknowledge",
                examples=[
                    {
                        "highlight": "acknowledging",
                        "context": "The report is acknowledging a known limitation.",
                    }
                ],
            )
        ],
        api_key="test-key",
    )
    assert result.cluster_id == "cluster-1"
    assert [payload["model"] for payload in payloads] == [
        ref_enrichment_prompts.DEFAULT_SEMANTIC_MODEL
    ]
    assert payloads[0]["reasoning"] == {"effort": "low", "exclude": True}
    assert payloads[0]["max_tokens"] == 16000
    assert payloads[0]["temperature"] == 0.1
    assert payloads[0]["response_format"]["json_schema"]["schema"]["properties"][
        "cluster_id"
    ]["enum"] == ["new_cluster", "cluster-1"]
    assert all(payload["provider"]["require_parameters"] for payload in payloads)


def test_enrichment_retries_five_times_with_varied_requests(monkeypatch) -> None:
    payloads = []

    def fail_request(payload, _api_key):
        payloads.append(payload)
        raise OSError("provider unavailable")

    monkeypatch.setattr(ref_enrichment_client, "_post_json", fail_request)
    monkeypatch.setattr(ref_enrich.time, "sleep", lambda _seconds: None)
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


def test_semantic_json_code_fence_is_removed_without_changing_json() -> None:
    value = '{"lemma":"robust"}'
    assert ref_enrichment_client._strip_json_code_fence(value) == value
    assert ref_enrichment_client._strip_json_code_fence(f"```json\n{value}\n```") == value


def test_approve_context_candidates_returns_only_approved_ids(monkeypatch) -> None:
    payloads = []
    candidates = [
        ContextCandidate(
            id="c1",
            surface="impaired",
            sentence="An impaired immune response follows quickly.",
        ),
        ContextCandidate(
            id="c2",
            surface="impairment",
            sentence="The review notes the same impairment in the discussion.",
        ),
        ContextCandidate(
            id="c3",
            surface="impaired",
            sentence="A different sense of impaired appears here.",
        ),
    ]

    def fake_request(payload, _api_key):
        payloads.append(payload)
        batch = {
            "items": [
                {"id": "c1", "suitable": True},
                {"id": "c2", "suitable": False},
                {"id": "c3", "suitable": True},
            ]
        }
        return {"choices": [{"message": {"content": json.dumps(batch)}}]}

    monkeypatch.setattr(ref_enrichment_client, "_post_json", fake_request)
    approved = ref_cards_llm_cache.approve_context_candidates(
        leader="impair",
        definition="weaken or damage something",
        translations=["ухудшать", "нарушать"],
        known_contexts=["Chronic stress can impair memory consolidation."],
        candidates=candidates,
        api_key="test-key",
    )

    assert approved == {"c1", "c3"}
    assert payloads[0]["temperature"] == 0.1
    assert payloads[0]["model"] == ref_enrichment_prompts.DEFAULT_SEMANTIC_MODEL
    assert payloads[0]["response_format"]["json_schema"]["schema"]["$defs"][
        "ContextApproval"
    ]["properties"]["suitable"]["type"] == "boolean"


def test_approve_context_candidates_retries_on_id_set_mismatch(monkeypatch) -> None:
    calls = []
    candidates = [
        ContextCandidate(id="c1", surface="impaired", sentence="Sentence one."),
        ContextCandidate(id="c2", surface="impairment", sentence="Sentence two."),
    ]

    def fake_request(payload, _api_key):
        calls.append(payload)
        batch = {"items": [{"id": "c9", "suitable": True}, {"id": "c2", "suitable": True}]}
        return {"choices": [{"message": {"content": json.dumps(batch)}}]}

    monkeypatch.setattr(ref_enrichment_client, "_post_json", fake_request)
    monkeypatch.setattr(ref_enrich.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="context approval failed after 3 attempts"):
        ref_cards_llm_cache.approve_context_candidates(
            leader="impair",
            definition="weaken",
            translations=["ухудшать"],
            known_contexts=[],
            candidates=candidates,
            api_key="test-key",
        )

    assert len(calls) == 3


def test_approve_context_candidates_accepts_empty_candidate_list() -> None:
    assert (
        ref_cards_llm_cache.approve_context_candidates(
            leader="impair",
            definition="weaken",
            translations=["ухудшать"],
            known_contexts=[],
            candidates=[],
            api_key="test-key",
        )
        == set()
    )
