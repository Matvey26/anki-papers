"""enrich."""
from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from articles_to_anki.enrichment import client
from articles_to_anki.enrichment.client import _apply_model_generation_config
from articles_to_anki.enrichment.prompts import (
    CACHE_VERSION,
    DEFAULT_MODEL,
    RETRY_INSTRUCTIONS,
    RETRY_TEMPERATURES,
    SYSTEM_PROMPT,
)
from articles_to_anki.models import (
    EnrichedItem,
    EnrichmentBatch,
    EnrichmentRequestItem,
    RecallDistractors,
    TargetContext,
)


def load_env_file(path: str | Path) -> None:
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("\"'")
        if name:
            os.environ.setdefault(name, value)


def enrich_targets(
    targets: list[TargetContext],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    batch_size: int = 1,
    cache_path: str | Path | None = None,
    max_attempts: int = 5,
) -> list[EnrichedItem]:
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is missing.")
    if batch_size < 1:
        raise ValueError("batch_size must be positive.")

    cache_file = Path(cache_path) if cache_path is not None else None
    cache = {
        key: value
        for key, value in _read_cache(cache_file).items()
        if key.startswith(f"{CACHE_VERSION}\0")
    }
    resolved: dict[str, EnrichedItem] = {}
    pending: list[TargetContext] = []
    for target in targets:
        cached = cache.get(_cache_key(model, target))
        if cached is None:
            pending.append(target)
        else:
            try:
                item = EnrichedItem.model_validate(cached)
            except ValidationError:
                pending.append(target)
            else:
                if item.id == target.id:
                    resolved[target.id] = item
                else:
                    pending.append(target)

    for offset in range(0, len(pending), batch_size):
        batch_targets = pending[offset : offset + batch_size]
        enriched = _request_batch(
            batch_targets,
            api_key=api_key,
            model=model,
            max_attempts=max_attempts,
        )
        for target in batch_targets:
            item = enriched[target.id]
            resolved[target.id] = item
            cache[_cache_key(model, target)] = item.model_dump()
        _write_cache(cache_file, cache)

    missing = [target.id for target in targets if target.id not in resolved]
    if missing:
        raise RuntimeError(f"Missing enrichments for ids: {missing}")
    return [resolved[target.id] for target in targets]


def build_openrouter_payload(
    requests: list[EnrichmentRequestItem], model: str
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"items": [item.model_dump() for item in requests]},
                    ensure_ascii=False,
                ),
            },
        ],
        "max_tokens": 2500,
        "stream": False,
        "plugins": [{"id": "response-healing"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "anki_vocabulary_enrichment",
                "strict": True,
                "schema": EnrichmentBatch.model_json_schema(),
            },
        },
        "provider": {"require_parameters": True},
    }
    _apply_model_generation_config(payload, model=model, temperature=0.2)
    return payload


def _request_batch(
    targets: list[TargetContext],
    *,
    api_key: str,
    model: str,
    max_attempts: int,
) -> dict[str, EnrichedItem]:
    requests = [
        EnrichmentRequestItem(id=item.id, target=item.target, sentence=item.sentence)
        for item in targets
    ]
    base_payload = build_openrouter_payload(requests, model)
    expected_ids = {item.id for item in requests}
    last_error: Exception | None = None
    previous_content: str | None = None

    for attempt in range(1, max_attempts + 1):
        payload = deepcopy(base_payload)
        if "temperature" in payload:
            payload["temperature"] = RETRY_TEMPERATURES[
                min(attempt - 1, len(RETRY_TEMPERATURES) - 1)
            ]
        if attempt > 1:
            retry_instruction = RETRY_INSTRUCTIONS[
                min(attempt - 2, len(RETRY_INSTRUCTIONS) - 1)
            ]
            if previous_content is not None:
                payload["messages"].append(
                    {"role": "assistant", "content": previous_content}
                )
            payload["messages"].append(
                {
                    "role": "user",
                    "content": (
                        f"{retry_instruction} Previous failure: "
                        f"{str(last_error)[:700]}"
                    ),
                }
            )
        content: str | None = None
        try:
            response = client._post_json(payload, api_key)
            content = response["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise RuntimeError("OpenRouter returned non-text structured content.")
            parsed = EnrichmentBatch.model_validate_json(content)
            by_id = {item.id: item for item in parsed.items}
            if len(parsed.items) != len(by_id):
                raise RuntimeError("OpenRouter returned duplicate ids.")
            if set(by_id) != expected_ids:
                raise RuntimeError(
                    "OpenRouter returned a different id set: "
                    f"expected {sorted(expected_ids)}, got {sorted(by_id)}"
                )
            target_by_id = {item.id: item.target for item in requests}
            for item in parsed.items:
                target_normalized = _letters_only(target_by_id[item.id])
                if any(
                    _letters_only(alternative) == target_normalized
                    for alternative in item.forbidden_alternatives_en
                ):
                    raise RuntimeError(
                        f"OpenRouter repeated target in alternatives for {item.id}."
                    )
            return by_id
        except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
            last_error = exc
            previous_content = content
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))

    raise RuntimeError(
        f"OpenRouter enrichment failed after {max_attempts} attempts: {last_error}"
    ) from last_error


def _validate_recall_distractors(
    target: str,
    distractors: RecallDistractors,
) -> None:
    normalized_target = " ".join(target.casefold().split())
    # Broad lists are hidden scratch work and may legitimately contain the
    # source form.  Only the curated lists reach the learner-facing card.
    values = distractors.valid_substitutes_en + distractors.valid_related_en
    if any(" ".join(value.casefold().split()) == normalized_target for value in values):
        raise RuntimeError("Recall distractors must not contain the target itself.")


def _cache_key(model: str, target: TargetContext) -> str:
    return (
        f"{CACHE_VERSION}\0{model}\0{target.id}\0"
        f"{target.target}\0{target.sentence}"
    )


def _letters_only(value: str) -> str:
    return "".join(char for char in value.casefold() if "a" <= char <= "z")


def _read_cache(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_cache(path: Path | None, cache: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
