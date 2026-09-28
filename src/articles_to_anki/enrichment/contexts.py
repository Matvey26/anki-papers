"""enrichment / contexts."""
from __future__ import annotations

import json
import time

from articles_to_anki.enrichment import client
from articles_to_anki.enrichment.client import (
    _apply_model_generation_config,
    _strip_json_code_fence,
)
from articles_to_anki.enrichment.prompts import (
    CONTEXT_APPROVAL_SYSTEM_PROMPT,
    DEFAULT_SEMANTIC_MODEL,
    MAX_APPROVAL_ATTEMPTS,
)
from articles_to_anki.models import ContextApprovalBatch, ContextCandidate


def approve_context_candidates(
    *,
    leader: str,
    definition: str,
    translations: list[str],
    known_contexts: list[str],
    candidates: list[ContextCandidate],
    api_key: str,
    model: str = DEFAULT_SEMANTIC_MODEL,
) -> set[str]:
    """Ask the model which real article sentences fit the card's exact sense."""
    if not candidates:
        return set()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CONTEXT_APPROVAL_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "leader": leader,
                        "definition": definition,
                        "translations_ru": translations,
                        "known_contexts": known_contexts,
                        "candidates": [
                            {
                                "id": item.id,
                                "surface": item.surface,
                                "sentence": item.sentence,
                            }
                            for item in candidates
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "max_tokens": 4000,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "article_context_approval",
                "strict": True,
                "schema": ContextApprovalBatch.model_json_schema(),
            },
        },
        "provider": {"require_parameters": True},
    }
    _apply_model_generation_config(payload, model=model, temperature=0.1)
    requested_ids = {item.id for item in candidates}
    last_error: Exception | None = None
    for attempt in range(1, MAX_APPROVAL_ATTEMPTS + 1):
        try:
            result = client._post_json(payload, api_key)
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise RuntimeError("OpenRouter returned non-text context approval.")
            batch = ContextApprovalBatch.model_validate_json(
                _strip_json_code_fence(content)
            )
            by_id = {item.id: item.suitable for item in batch.items}
            if len(by_id) != len(batch.items):
                raise RuntimeError("OpenRouter returned duplicate candidate ids.")
            if set(by_id) != requested_ids:
                raise RuntimeError(
                    "OpenRouter changed the candidate id set: "
                    f"expected {sorted(requested_ids)}, got {sorted(by_id)}"
                )
            return {item_id for item_id, suitable in by_id.items() if suitable}
        except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
            last_error = exc
            if attempt < MAX_APPROVAL_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(
        f"OpenRouter context approval failed after {MAX_APPROVAL_ATTEMPTS} attempts: "
        f"{last_error}"
    ) from last_error
