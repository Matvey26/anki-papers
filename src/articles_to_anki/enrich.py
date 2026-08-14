from __future__ import annotations

from copy import deepcopy
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import (
    EnrichedItem,
    EnrichmentBatch,
    EnrichmentRequestItem,
    TargetContext,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-5.6-luna"
CACHE_VERSION = "v4"
RETRY_TEMPERATURES = (0.2, 0.1, 0.3, 0.0, 0.25)
RETRY_INSTRUCTIONS = (
    "Retry independently. Re-read the sentence before choosing the target's exact sense.",
    "Start over with different wording. Draft each field mentally, then emit only valid JSON.",
    "Prioritize schema validity and exact property names; use conservative common translations.",
    "Final attempt: solve one item at a time, verify every constraint, then return the schema.",
)

SYSTEM_PROMPT = """\
You create English-to-Russian vocabulary cards for an advanced English learner.
For every input item:
1. Preserve its id exactly.
2. First disambiguate the highlighted target from the FULL sentence. In
   context_explanation_ru, briefly explain in Russian which exact sense applies and name the
   nearby clue, collocation, or grammatical construction that establishes it.
3. Give translations_ru as 2-5 UNIQUE Russian translations or close variants. EVERY option
   must express the SAME meaning that the target has in this exact sentence and be acceptable
   on the back of the card. Never include unrelated dictionary senses merely to add variety.
4. Give replacement_ru in the exact Russian grammatical form that can replace the English
   target inside the otherwise English sentence. Preserve tense, number, and discourse role.
   Mentally substitute it into the full sentence and verify that the sentence's intended
   meaning remains intact. Do not add a comma, period, colon, semicolon, exclamation mark, or
   question mark after replacement_ru: punctuation adjacent to the target is already preserved.
5. Give 2-6 UNIQUE, simpler English NEAR-SYNONYMS that a learner might guess in this exact
   context but must not use. "Forbidden" means forbidden as the CARD ANSWER, not opposite in
   meaning. First substitute each candidate for the target in the original sentence. Keep it
   only if the resulting sentence remains grammatical and states substantially the SAME claim.
   NEVER give antonyms, opposites, unrelated words, Russian words, the target itself, or trivial
   spelling/case variants. For "thoroughly validated", valid alternatives include "fully",
   "carefully", and "extensively"; "slightly", "partially", and "superficially" are invalid.
   For "impede", valid alternatives include "hinder", "block", and "slow down";
   "accelerate" is invalid because it reverses the meaning.
6. Treat a multiword target as one expression.
7. context_explanation_ru, every item in translations_ru, and replacement_ru MUST be written
   in Russian Cyrillic. Never leave the English target in replacement_ru.
The object for each item MUST contain exactly these five property names: id,
context_explanation_ru, translations_ru, replacement_ru, forbidden_alternatives_en. Never
rename any property. The near-synonym list is ALWAYS stored under the literal property name
forbidden_alternatives_en; never rename it to near_synonyms or anything else.
Return every input id once and only once. Follow the supplied JSON Schema exactly.
"""


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
        "temperature": 0.2,
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
    if model.startswith("openai/gpt-5.6-luna"):
        # Luna's OpenRouter endpoint supports strict structured output but not
        # temperature; require_parameters would otherwise reject all providers.
        payload.pop("temperature")
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
            response = _post_json(payload, api_key)
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


def _post_json(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/local/articles-to-anki",
            "X-Title": "Articles to Anki",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"OpenRouter HTTP {exc.code}: {error_body[:1000]}"
        ) from exc
    return json.loads(body)


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
