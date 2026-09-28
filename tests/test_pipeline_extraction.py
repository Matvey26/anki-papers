from __future__ import annotations

from pathlib import Path

from support_pipeline import token

import articles_to_anki.extraction.document as ref_extraction_document
from articles_to_anki.extract import (
    RECALL_PLACEHOLDER,
    DocumentText,
    ExtractionConfig,
    _context_from_document_text,
    _extract_document_text,
    _group_selected_tokens,
    _pdf_quad_to_region,
    _rectangle_overlap_ratio,
    find_article_contexts,
    find_variant_article_contexts,
    is_sentence_end,
    render_sentence,
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


def test_two_word_phrase_can_cross_a_line_wrap() -> None:
    tokens = [token("along", 0, x0=498), token("with", 1, x0=70)]
    tokens[1].top = 113
    tokens[1].bottom = 124

    groups = _group_selected_tokens(tokens, config=ExtractionConfig())

    assert len(groups) == 1
    assert groups[0].target == "along with"


def test_cross_page_context_discards_footnote_urls_and_is_clipped(monkeypatch) -> None:
    class FakePage:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class FakeReader:
        def __init__(self, _path: Path) -> None:
            self.pages = [
                FakePage(
                    "• Open-source models include general models like DeepSeek-LLM, Qwen, "
                    "and (4)\n5https://open.bigmodel.cn/dev/api#glm-4\n10"
                ),
                FakePage(
                    "ChatGLM3 6B, as well as models with enhancements in mathematics "
                    + "including many separately evaluated systems, " * 20
                    + "which conclude the comparison."
                ),
            ]

    monkeypatch.setattr(ref_extraction_document, "PdfReader", FakeReader)
    document = _extract_document_text(Path("ignored.pdf"))
    context = _context_from_document_text(document, "as well as", 1, 0)

    assert context is not None
    sentence, sentence_html, _ = context
    assert "open.bigmodel.cn" not in sentence
    assert len(sentence) <= 424
    assert "<b>as well as</b>" in sentence_html


def test_article_context_finds_unhighlighted_phrase() -> None:
    sentence = (
        "However, open-source models considerably trail behind in performance. "
        "A separate result follows."
    )
    document = DocumentText(
        text=sentence,
        page_ranges=[(0, len(sentence))],
        hard_page_starts=[False],
    )

    contexts = find_article_contexts(document, ["trail behind"])

    assert len(contexts) == 1
    assert contexts[0].target == "trail behind"
    assert contexts[0].source_page == 1


def test_article_context_prefers_longest_overlapping_phrase() -> None:
    sentence = "The benchmark covers code tasks, as well as multilingual reasoning."
    document = DocumentText(
        text=sentence,
        page_ranges=[(0, len(sentence))],
        hard_page_starts=[False],
    )

    contexts = find_article_contexts(document, ["as well", "as well as"])

    assert [(context.target, context.sentence) for context in contexts] == [
        ("as well as", sentence)
    ]


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


def test_variant_article_context_finds_inflected_forms() -> None:
    sentence = (
        "Chronic stress can impair memory consolidation. "
        "An impaired immune response follows quickly. "
        "The review notes the same impairment in the discussion."
    )
    document = DocumentText(
        text=sentence,
        page_ranges=[(0, len(sentence))],
        hard_page_starts=[False],
    )

    contexts = find_variant_article_contexts(document, ["impair"])

    assert [context.target for context in contexts] == [
        "impaired",
        "impairment",
    ]
    assert all("impair" in context.sentence for context in contexts)


def test_variant_article_context_finds_inflected_phrases() -> None:
    sentence = (
        "The small model trailed behind the leader. "
        "The baseline trails behind in the last benchmark."
    )
    document = DocumentText(
        text=sentence,
        page_ranges=[(0, len(sentence))],
        hard_page_starts=[False],
    )

    contexts = find_variant_article_contexts(document, ["trail behind"])

    assert [context.target for context in contexts] == [
        "trailed behind",
        "trails behind",
    ]
    assert "<b>trailed behind</b>" in contexts[0].sentence_html


def test_variant_article_context_skips_exact_and_unrelated_words() -> None:
    sentence = (
        "The agent improves the policy. "
        "An improved policy is deployed. "
        "The improvement was measured."
    )
    document = DocumentText(
        text=sentence,
        page_ranges=[(0, len(sentence))],
        hard_page_starts=[False],
    )

    contexts = find_variant_article_contexts(document, ["improve"])

    assert sorted(context.target for context in contexts) == [
        "improved",
        "improvement",
        "improves",
    ]
