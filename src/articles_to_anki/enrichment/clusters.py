"""enrichment / clusters."""
from __future__ import annotations

import json

from articles_to_anki.enrichment import client
from articles_to_anki.enrichment.client import _strip_json_code_fence
from articles_to_anki.enrichment.prompts import (
    CLUSTER_SYSTEM_PROMPT,
    DEFAULT_SEMANTIC_MODEL,
)
from articles_to_anki.enrichment.targets import _validate_recall_distractors
from articles_to_anki.models import ClusterAnalysis, ClusterCandidate


def analyse_cluster_assignment(
    target: str,
    normalized_target: str,
    sentence: str,
    candidates: list[ClusterCandidate],
    *,
    api_key: str,
    model: str = DEFAULT_SEMANTIC_MODEL,
) -> ClusterAnalysis:
    """Choose a cluster and create its card context in one model call."""
    allowed_cluster_ids = ["new_cluster", *(item.cluster_id for item in candidates)]
    schema = ClusterAnalysis.model_json_schema()
    schema["properties"]["cluster_id"]["enum"] = allowed_cluster_ids
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CLUSTER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "highlight": target,
                        "normalized_highlight": normalized_target,
                        "context": sentence,
                        "candidate_clusters": [
                            item.model_dump() for item in candidates
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "max_tokens": 16000,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "highlight_cluster_assignment",
                "strict": True,
                "schema": schema,
            },
        },
        "provider": {"require_parameters": True},
        "reasoning": {"effort": "low", "exclude": True},
    }
    if not model.startswith("openai/gpt-5.6-luna"):
        payload["temperature"] = 0.1
    result = client._post_json(payload, api_key)
    content = result["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise RuntimeError("OpenRouter returned non-text cluster analysis.")
    analysis = ClusterAnalysis.model_validate_json(_strip_json_code_fence(content))
    if analysis.cluster_id not in allowed_cluster_ids:
        raise RuntimeError("OpenRouter selected a cluster outside the candidate set.")
    _validate_recall_distractors(target, analysis.source_distractors)
    return analysis
