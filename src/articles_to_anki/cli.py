from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

from .enrich import DEFAULT_MODEL, enrich_targets, load_env_file
from .export import write_anki_csv, write_extraction_json
from .extract import ExtractionConfig, extract_targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract yellow brush highlights from a PDF and create Anki CSV cards."
        )
    )
    parser.add_argument("pdf", type=Path, help="Input PDF.")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="Output directory. Default: output/<pdf-stem>.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="File containing OPENROUTER_API_KEY and optionally OPENROUTER_MODEL.",
    )
    parser.add_argument("--model", help="Override the OpenRouter model.")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument(
        "--shuffle-seed",
        type=int,
        help=(
            "Seed for reproducible CSV card order. By default a fresh random seed "
            "is generated and recorded in run.json."
        ),
    )
    parser.add_argument(
        "--extract-only",
        action="store_true",
        help="Do not call OpenRouter and do not create the final CSV.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Write page images with accepted and rejected detection boxes.",
    )
    parser.add_argument("--render-dpi", type=int, default=216)
    parser.add_argument("--min-coverage", type=float, default=0.60)
    parser.add_argument("--max-vertical-spill", type=float, default=0.30)
    parser.add_argument(
        "--exclude-targets-from",
        type=Path,
        action="append",
        default=[],
        metavar="EXTRACTED_TARGETS_JSON",
        help=(
            "Skip words/phrases already present in another extracted_targets.json. "
            "May be supplied more than once. Repeated highlights in the current PDF "
            "are preserved as separate contexts."
        ),
    )
    return parser


def _target_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _load_excluded_targets(paths: list[Path]) -> set[str]:
    excluded: set[str] = set()
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            targets = document["targets"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SystemExit(f"Cannot read deduplication source {path}: {exc}") from exc
        if not isinstance(targets, list):
            raise SystemExit(f"Invalid deduplication source {path}: targets must be a list.")
        for item in targets:
            if isinstance(item, dict) and isinstance(item.get("target"), str):
                excluded.add(_target_key(item["target"]))
    return excluded


def _exclude_processed_targets(targets, excluded: set[str]):
    kept = []
    skipped = []
    for target in targets:
        key = _target_key(target.target)
        if key in excluded:
            skipped.append(target)
            continue
        kept.append(target)
    return kept, skipped


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pdf_path = args.pdf.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else (Path.cwd() / "output" / pdf_path.stem).resolve()
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    config = ExtractionConfig(
        render_dpi=args.render_dpi,
        min_coverage=args.min_coverage,
        max_vertical_spill=args.max_vertical_spill,
    )
    debug_dir = output_dir / "debug" if args.debug else None
    targets = extract_targets(pdf_path, config=config, debug_dir=debug_dir)
    excluded = _load_excluded_targets(args.exclude_targets_from)
    targets, skipped_targets = _exclude_processed_targets(targets, excluded)
    extraction_path = write_extraction_json(
        output_dir / "extracted_targets.json", targets
    )

    print(f"Detected targets: {len(targets)}")
    for target in targets:
        print(f"  p.{target.source_page}: {target.target}")
    print(f"Extraction data: {extraction_path}")
    if skipped_targets:
        print(f"Skipped duplicate targets: {len(skipped_targets)}")
        for target in skipped_targets:
            print(f"  p.{target.source_page}: {target.target}")

    if args.extract_only:
        return 0
    if not targets:
        raise SystemExit("No highlighted targets found; CSV was not created.")

    load_env_file(args.env_file)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise SystemExit(
            f"OPENROUTER_API_KEY is not set (checked {args.env_file})."
        )
    model = args.model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
    enrichments = enrich_targets(
        targets,
        api_key=api_key,
        model=model,
        batch_size=args.batch_size,
        cache_path=output_dir / "enrichment_cache.json",
    )
    shuffle_seed = (
        args.shuffle_seed
        if args.shuffle_seed is not None
        else secrets.randbits(64)
    )
    csv_path = write_anki_csv(
        output_dir / "anki_cards.csv",
        targets,
        enrichments,
        article_tag=pdf_path.stem,
        shuffle_seed=shuffle_seed,
    )
    metadata_path = output_dir / "run.json"
    metadata_path.write_text(
        json.dumps(
            {
                "source_pdf": str(pdf_path),
                "model": model,
                "targets": len(targets),
                "cards": len(targets) * 2,
                "shuffle_seed": shuffle_seed,
                "csv": str(csv_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Anki cards: {len(targets) * 2}")
    print(f"CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
