from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from anki.collection import Collection
from anki.decks import DeckId
from anki.exporting import AnkiPackageExporter
from anki.import_export_pb2 import (
    ImportAnkiPackageOptions,
    ImportAnkiPackageRequest,
)
from anki.utils import base91
from anki_papers_sync_worker.official import _semantic_sides

from articles_to_anki.enrich import (
    DEFAULT_SEMANTIC_MODEL,
    analyse_semantic_context,
    load_env_file,
    select_semantic_match,
)
from articles_to_anki.models import SemanticAnalysis, SemanticCandidate, SemanticMatch
from articles_to_anki.webapp import (
    canonical_semantic_family,
    merge_semantic_translations,
    semantic_family_prefix,
)

BOLD_RE = re.compile(r"<b\b[^>]*>(.*?)</b>", re.IGNORECASE | re.DOTALL)
BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
SMALL_TAIL_RE = re.compile(
    r"<br\s*/?><small>.*?</small>\s*$",
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class SourceContext:
    id: str
    target: str
    sentence: str
    translations: list[str]
    replacement: str
    source: str
    note_ids: list[int]
    learned_cards: int


def plain_text(value: str) -> str:
    with_breaks = BREAK_RE.sub(" ", value)
    return " ".join(html.unescape(TAG_RE.sub(" ", with_breaks)).split())


def normalized(value: str) -> str:
    normalized_value = " ".join(value.casefold().split())
    # PDF text extraction often adds spaces before punctuation or just inside
    # parentheses. Those layout artifacts must not create duplicate contexts.
    normalized_value = re.sub(r"\s+([,.;:!?%)\]])", r"\1", normalized_value)
    return re.sub(r"([(\[])\s+", r"\1", normalized_value)


def note_direction(tags: list[str]) -> str | None:
    for prefix in ("direction::", "card::"):
        for tag in tags:
            value = tag.removeprefix(prefix)
            if tag.startswith(prefix) and value in {"meaning", "recall"}:
                return value
    return None


def translations_from_back(value: str) -> list[str]:
    translations: list[str] = []
    for part in BREAK_RE.split(value):
        cleaned = plain_text(part).lstrip("•·- ").strip()
        if cleaned and cleaned.casefold() not in {item.casefold() for item in translations}:
            translations.append(cleaned)
    return translations


def source_label(tags: list[str]) -> str:
    labels = sorted(
        tag
        for tag in tags
        if tag.startswith(("article::", "page::"))
    )
    return " · ".join(labels) if labels else "original_deck"


def extract_source_contexts(
    collection_path: Path,
    *,
    source_deck_name: str | None = None,
) -> list[SourceContext]:
    collection = Collection(str(collection_path))
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        if source_deck_name is None:
            note_ids = collection.find_notes("")
        else:
            source_deck = collection.decks.by_name(source_deck_name)
            if source_deck is None:
                raise ValueError(f"Source deck does not exist: {source_deck_name}")
            note_ids = sorted(
                {
                    int(collection.get_card(card_id).nid)
                    for card_id in collection.find_cards(f"did:{source_deck['id']}")
                }
            )
        for note_id in note_ids:
            note = collection.get_note(note_id)
            if len(note.fields) < 2:
                continue
            front, back = note.fields[:2]
            direction = note_direction(note.tags)
            bold = BOLD_RE.search(front)
            replacement = ""
            translations: list[str] = []
            if direction == "meaning" and bold is not None:
                target = plain_text(bold.group(1))
                sentence = plain_text(front)
                translations = translations_from_back(back)
            elif direction == "recall" and bold is not None:
                target = plain_text(back)
                clean_front = SMALL_TAIL_RE.sub("", front)
                bold = BOLD_RE.search(clean_front)
                if bold is None:
                    continue
                replacement = plain_text(bold.group(1))
                sentence = plain_text(
                    clean_front[: bold.start()] + target + clean_front[bold.end() :]
                )
            else:
                continue
            if not target or not sentence:
                continue
            key = (normalized(target), normalized(sentence))
            entry = grouped.setdefault(
                key,
                {
                    "target": target,
                    "sentence": sentence,
                    "translations": [],
                    "replacement": "",
                    "sources": set(),
                    "note_ids": [],
                    "learned_cards": 0,
                },
            )
            entry["translations"] = merge_semantic_translations(
                entry["translations"], translations
            )
            if replacement and not entry["replacement"]:
                entry["replacement"] = replacement
            entry["sources"].add(source_label(note.tags))
            entry["note_ids"].append(int(note.id))
            entry["learned_cards"] += sum(card.reps > 0 for card in note.cards())
    finally:
        collection.close()

    contexts: list[SourceContext] = []
    for key, entry in grouped.items():
        digest = hashlib.sha256("\0".join(key).encode()).hexdigest()[:20]
        contexts.append(
            SourceContext(
                id=f"source:{digest}",
                target=entry["target"],
                sentence=entry["sentence"],
                translations=entry["translations"],
                replacement=entry["replacement"],
                source=" | ".join(sorted(entry["sources"])),
                note_ids=sorted(entry["note_ids"]),
                learned_cards=entry["learned_cards"],
            )
        )
    return sorted(contexts, key=lambda item: min(item.note_ids))


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def retry(operation, *, attempts: int = 4):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - provider errors need retry
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def analyse_contexts(
    contexts: list[SourceContext],
    *,
    api_key: str,
    model: str,
    cache_path: Path,
    workers: int,
) -> dict[str, SemanticAnalysis]:
    if cache_path.is_file():
        raw_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        raw_cache = {}
    analyses: dict[str, SemanticAnalysis] = {}
    for key, value in raw_cache.items():
        try:
            analyses[key] = SemanticAnalysis.model_validate(value)
        except ValueError:
            # Schema hardening intentionally invalidates stale provider output.
            continue
    pending = [context for context in contexts if context.id not in analyses]
    if not pending:
        return analyses

    def run(context: SourceContext) -> tuple[str, SemanticAnalysis]:
        analysis = retry(
            lambda: analyse_semantic_context(
                context.target,
                context.sentence,
                api_key=api_key,
                model=model,
            )
        )
        return context.id, analysis

    completed = len(contexts) - len(pending)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run, context): context for context in pending}
        for future in as_completed(futures):
            context_id, analysis = future.result()
            analyses[context_id] = analysis
            completed += 1
            write_json(
                cache_path,
                {key: value.model_dump() for key, value in analyses.items()},
            )
            print(
                f"analysis {completed}/{len(contexts)}: "
                f"{analysis.lemma} [{analysis.family_key}]",
                flush=True,
            )
    return analyses


def cluster_prefixes(cluster: dict[str, Any]) -> set[str]:
    return {
        semantic_family_prefix(value)
        for value in [cluster["family_key"], *cluster["lemmas"]]
        if value
    }


def candidate_clusters(
    analysis: SemanticAnalysis,
    clusters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_prefixes = {
        semantic_family_prefix(analysis.family_key),
        semantic_family_prefix(analysis.lemma),
    }
    candidates = [
        cluster
        for cluster in clusters
        if source_prefixes & cluster_prefixes(cluster)
    ]
    return candidates[-20:]


def semantic_candidate(cluster: dict[str, Any]) -> SemanticCandidate:
    return SemanticCandidate(
        id=cluster["id"],
        family_key=cluster["family_key"],
        lemmas=cluster["lemmas"][:12],
        parts_of_speech=cluster["parts_of_speech"][:6],
        sense_definition_en=cluster["sense_definition_en"],
    )


def add_unique(values: list[str], value: str) -> None:
    if value and value.casefold() not in {item.casefold() for item in values}:
        values.append(value)


def make_contexts(
    source: SourceContext,
    analysis: SemanticAnalysis,
) -> list[dict[str, Any]]:
    common = {
        "lemma": analysis.lemma,
        "family_key": analysis.family_key,
        "part_of_speech": analysis.part_of_speech,
        "sense_definition_en": analysis.sense_definition_en,
        "translations": analysis.translations_ru,
    }
    return [
        {
            **common,
            "id": source.id,
            "source": source.source,
            "target": source.target,
            "sentence": source.sentence,
            "replacement": analysis.replacement_ru,
            "valid_substitutes_en": analysis.source_distractors.valid_substitutes_en,
            "related_but_uninsertable_en": (
                analysis.source_distractors.related_but_uninsertable_en
            ),
        },
        {
            **common,
            "id": f"generated:{source.id}",
            "source": "llm_generated",
            "target": analysis.generated_surface,
            "sentence": analysis.generated_sentence,
            "replacement": analysis.generated_translation_ru,
            "valid_substitutes_en": analysis.generated_distractors.valid_substitutes_en,
            "related_but_uninsertable_en": (
                analysis.generated_distractors.related_but_uninsertable_en
            ),
        },
    ]


def build_clusters(
    contexts: list[SourceContext],
    analyses: dict[str, SemanticAnalysis],
    *,
    api_key: str,
    model: str,
    progress_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    clusters: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for index, source in enumerate(contexts, start=1):
        analysis = analyses[source.id]
        possible = candidate_clusters(analysis, clusters)
        candidates = [semantic_candidate(cluster) for cluster in possible]
        match: SemanticMatch = retry(
            lambda current_analysis=analysis, current_candidates=candidates: select_semantic_match(
                current_analysis,
                current_candidates,
                api_key=api_key,
                model=model,
            )
        )
        selected = next(
            (cluster for cluster in possible if cluster["id"] == match.card_id),
            None,
        )
        if selected is None:
            cluster_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"anki-papers-semantic-preview:{source.id}",
                )
            )
            selected = {
                "id": cluster_id,
                "family_key": analysis.family_key,
                "lemmas": [analysis.lemma],
                "parts_of_speech": [analysis.part_of_speech],
                "sense_definition_en": analysis.sense_definition_en,
                "translations": merge_semantic_translations(
                    source.translations,
                    analysis.translations_ru,
                ),
                "contexts": make_contexts(source, analysis),
                "source_context_ids": [source.id],
                "source_note_ids": source.note_ids,
                "learned_source_cards": source.learned_cards,
            }
            clusters.append(selected)
        else:
            selected["family_key"] = canonical_semantic_family(
                selected["family_key"], analysis.family_key
            )
            add_unique(selected["lemmas"], analysis.lemma)
            add_unique(selected["parts_of_speech"], analysis.part_of_speech)
            selected["sense_definition_en"] = match.merged_sense_definition_en
            selected["translations"] = merge_semantic_translations(
                selected["translations"],
                source.translations,
                analysis.translations_ru,
            )
            selected["contexts"].extend(make_contexts(source, analysis))
            selected["source_context_ids"].append(source.id)
            selected["source_note_ids"].extend(source.note_ids)
            selected["learned_source_cards"] += source.learned_cards
        decisions.append(
            {
                "source_context_id": source.id,
                "target": source.target,
                "sentence": source.sentence,
                "analysis": analysis.model_dump(),
                "candidate_ids": [candidate.id for candidate in candidates],
                "match": match.model_dump(),
                "result_cluster_id": selected["id"],
            }
        )
        write_json(
            progress_path,
            {"clusters": clusters, "decisions": decisions},
        )
        print(
            f"cluster {index}/{len(contexts)}: {source.target} -> "
            f"{selected['family_key']} ({match.relationship})",
            flush=True,
        )
    return clusters, decisions


def create_notetype(collection: Collection) -> dict[str, Any]:
    notetype = collection.models.new("Anki Papers Semantic Preview")
    collection.models.add_field(notetype, collection.models.new_field("Front"))
    collection.models.add_field(notetype, collection.models.new_field("Back"))
    template = collection.models.new_template("Card 1")
    template["qfmt"] = "{{Front}}"
    template["afmt"] = '{{FrontSide}}<hr id="answer">{{Back}}'
    collection.models.add_template(notetype, template)
    collection.models.add(notetype)
    return notetype


def safe_tag(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_:-]+", "_", value).strip("_") or "word"


def build_apkg(
    clusters: list[dict[str, Any]],
    *,
    output_path: Path,
    deck_name: str,
    guid_namespace: str = "semantic-preview-v1",
) -> None:
    collection_path = output_path.with_name("semantic-preview-build.anki2")
    collection_path.unlink(missing_ok=True)
    output_path.unlink(missing_ok=True)
    collection = Collection(str(collection_path))
    try:
        deck_id = collection.decks.id(deck_name)
        if deck_id is None:
            raise RuntimeError("Could not create preview deck")
        notetype = create_notetype(collection)
        for cluster in clusters:
            card = {
                "id": cluster["id"],
                "semantic": True,
                "lemma": cluster["lemmas"][0],
                "family_key": cluster["family_key"],
                "sense_definition_en": cluster["sense_definition_en"],
                "translations": cluster["translations"],
                "contexts": cluster["contexts"],
            }
            for direction in ("meaning", "recall"):
                front, back = _semantic_sides(card, direction)
                note = collection.new_note(notetype)
                note.guid = base91(
                    int.from_bytes(
                        hashlib.sha256(
                            f"{guid_namespace}:{cluster['id']}:{direction}".encode()
                        ).digest()[:8],
                        "big",
                    )
                )
                note.fields[0] = front
                note.fields[1] = back
                note.tags = [
                    "semantic_preview",
                    f"anki_papers::{cluster['id']}",
                    f"family::{safe_tag(cluster['family_key'])}",
                    f"direction::{direction}",
                ]
                collection.add_note(note, DeckId(deck_id))
        exporter = AnkiPackageExporter(collection)
        exporter.did = deck_id
        exporter.includeSched = False
        exporter.includeMedia = False
        exporter.exportInto(str(output_path))
    finally:
        collection.close()
        collection_path.unlink(missing_ok=True)


def verify_apkg(path: Path, *, deck_name: str, expected_clusters: int) -> dict[str, Any]:
    # Verify the user-visible operation, not merely the archive contents: import
    # the package through Anki's official backend into a fresh collection.
    with tempfile.TemporaryDirectory(prefix="anki-semantic-import-check-") as temp_dir:
        collection = Collection(str(Path(temp_dir) / "collection.anki2"))
        try:
            collection.import_anki_package(
                ImportAnkiPackageRequest(
                    package_path=str(path.resolve()),
                    options=ImportAnkiPackageOptions(
                        merge_notetypes=True,
                        with_scheduling=False,
                        with_deck_configs=False,
                    ),
                )
            )
            target_deck = collection.decks.by_name(deck_name)
            if target_deck is None:
                deck_names = [
                    item.name for item in collection.decks.all_names_and_ids()
                ]
                raise RuntimeError(f"Preview deck missing after import: {deck_names}")
            target_cards = collection.find_cards(f"did:{target_deck['id']}")
            cards = collection.find_cards("")
            notes = collection.find_notes("")
            expected_notes = expected_clusters * 2
            if (
                len(target_cards) != expected_notes
                or len(cards) != expected_notes
                or len(notes) != expected_notes
            ):
                raise RuntimeError(
                    "Unexpected imported preview size: "
                    f"{len(notes)} notes, {len(cards)} cards, "
                    f"{len(target_cards)} target-deck cards"
                )
            empty_fields = sum(
                any(not field for field in collection.get_note(note_id).fields[:2])
                for note_id in notes
            )
            if empty_fields:
                raise RuntimeError(f"Preview contains {empty_fields} empty notes")
            source_deck = collection.decks.by_name("По умолчанию")
            source_deck_cards = (
                len(collection.find_cards(f"did:{source_deck['id']}"))
                if source_deck is not None
                else 0
            )
            if source_deck_cards:
                raise RuntimeError(
                    f"Source deck leaked into preview: {source_deck_cards} cards"
                )
            return {
                "import_succeeded": True,
                "deck_name": deck_name,
                "deck_names": [
                    item.name for item in collection.decks.all_names_and_ids()
                ],
                "notes": len(notes),
                "cards": len(cards),
                "target_deck_cards": len(target_cards),
                "source_default_deck_cards": source_deck_cards,
                "clusters": expected_clusters,
                "empty_notes": empty_fields,
            }
        finally:
            collection.close()


def write_report(path: Path, clusters: list[dict[str, Any]]) -> None:
    import csv

    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "cluster_id",
                "family_key",
                "lemmas",
                "parts_of_speech",
                "source_contexts",
                "source_targets",
                "translations_ru",
                "sense_definition_en",
                "learned_source_cards",
            ],
        )
        writer.writeheader()
        for cluster in clusters:
            source_contexts = [
                context
                for context in cluster["contexts"]
                if context["source"] != "llm_generated"
            ]
            writer.writerow(
                {
                    "cluster_id": cluster["id"],
                    "family_key": cluster["family_key"],
                    "lemmas": " | ".join(cluster["lemmas"]),
                    "parts_of_speech": " | ".join(cluster["parts_of_speech"]),
                    "source_contexts": len(source_contexts),
                    "source_targets": " | ".join(
                        dict.fromkeys(context["target"] for context in source_contexts)
                    ),
                    "translations_ru": " | ".join(cluster["translations"]),
                    "sense_definition_en": cluster["sense_definition_en"],
                    "learned_source_cards": cluster["learned_source_cards"],
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--deck-name",
        default="Anki Papers Semantic Dedup Preview — 2026-08-15",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--source-deck")
    parser.add_argument(
        "--guid-namespace",
        default="semantic-preview-v1",
        help="Change this when a preview must coexist with an older imported package.",
    )
    parser.add_argument(
        "--output-name",
        default="anki-papers-semantic-dedup-preview.apkg",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    load_env_file(args.env_file)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is missing")
    model = os.environ.get("OPENROUTER_SEMANTIC_MODEL", DEFAULT_SEMANTIC_MODEL)

    contexts = extract_source_contexts(
        args.source.resolve(),
        source_deck_name=args.source_deck,
    )
    write_json(
        args.output_dir / "source-contexts.json",
        [asdict(context) for context in contexts],
    )
    print(f"exact source contexts: {len(contexts)}", flush=True)
    analyses = analyse_contexts(
        contexts,
        api_key=api_key,
        model=model,
        cache_path=args.output_dir / "semantic-analysis-cache.json",
        workers=max(1, args.workers),
    )
    clusters, decisions = build_clusters(
        contexts,
        analyses,
        api_key=api_key,
        model=model,
        progress_path=args.output_dir / "semantic-cluster-decisions.json",
    )
    manifest = {
        "deck_name": args.deck_name,
        "model": model,
        "source_contexts": len(contexts),
        "clusters": clusters,
        "decisions": decisions,
    }
    write_json(args.output_dir / "semantic-manifest.json", manifest)
    write_report(args.output_dir / "semantic-dedup-report.csv", clusters)
    output_path = args.output_dir / args.output_name
    build_apkg(
        clusters,
        output_path=output_path,
        deck_name=args.deck_name,
        guid_namespace=args.guid_namespace,
    )
    verification = verify_apkg(
        output_path,
        deck_name=args.deck_name,
        expected_clusters=len(clusters),
    )
    write_json(args.output_dir / "verification.json", verification)
    print(json.dumps(verification, ensure_ascii=False), flush=True)
    print(output_path.resolve(), flush=True)


if __name__ == "__main__":
    main()
