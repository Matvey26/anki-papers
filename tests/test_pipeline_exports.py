from __future__ import annotations

import csv

from articles_to_anki.cli import _exclude_processed_targets, _load_excluded_targets
from articles_to_anki.export import write_anki_csv
from articles_to_anki.extract import (
    RECALL_PLACEHOLDER,
)
from articles_to_anki.models import (
    EnrichedItem,
    TargetContext,
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
