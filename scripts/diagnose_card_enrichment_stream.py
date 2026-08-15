from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

from benchmark_card_enrichment_models import CASES, write_json

from articles_to_anki.enrich import (
    OPENROUTER_URL,
    build_openrouter_payload,
    load_env_file,
)
from articles_to_anki.models import EnrichmentBatch, EnrichmentRequestItem


def stream_case(
    case: dict[str, Any],
    model: str,
    api_key: str,
    *,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    payload = build_openrouter_payload(
        [
            EnrichmentRequestItem(
                id=case["id"],
                target=case["target"],
                sentence=case["sentence"],
            )
        ],
        model,
    )
    payload["stream"] = True
    if reasoning_effort is not None:
        payload["reasoning"] = {"effort": reasoning_effort}
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    metadata: dict[str, Any] = {}
    with urllib.request.urlopen(request, timeout=90) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            for key in ("id", "model", "provider", "usage"):
                if key in chunk:
                    metadata[key] = chunk[key]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            reasoning = delta.get("reasoning")
            if isinstance(content, str):
                content_parts.append(content)
            if isinstance(reasoning, str):
                reasoning_parts.append(reasoning)

    content = "".join(content_parts)
    result: dict[str, Any] = {
        "case_id": case["id"],
        "model_id": model,
        "reasoning_effort": reasoning_effort,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "metadata": metadata,
        "content": content,
        "reasoning_length": len("".join(reasoning_parts)),
    }
    try:
        result["parsed"] = EnrichmentBatch.model_validate_json(content).model_dump()
    except Exception as exc:  # noqa: BLE001 - preserve diagnostic failure
        result["validation_error"] = f"{type(exc).__name__}: {exc}"
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--case", action="append", required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reasoning-effort")
    args = parser.parse_args()

    load_env_file(args.env_file)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is missing")

    cases_by_id = {case["id"]: case for case in CASES}
    unknown = sorted(set(args.case) - cases_by_id.keys())
    if unknown:
        raise SystemExit(f"Unknown cases: {unknown}")

    report = {
        "diagnostic_only": True,
        "stream": True,
        "production_score_unchanged": True,
        "results": [],
    }
    for case_id in args.case:
        result = stream_case(
            cases_by_id[case_id],
            args.model,
            api_key,
            reasoning_effort=args.reasoning_effort,
        )
        report["results"].append(result)
        write_json(args.output, report)
        print(
            f"{case_id}: content={len(result['content'])} chars, "
            f"reasoning={result['reasoning_length']} chars, "
            f"valid={'parsed' in result}, {result['elapsed_seconds']}s",
            flush=True,
        )


if __name__ == "__main__":
    main()
