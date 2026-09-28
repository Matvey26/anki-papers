"""cards / llm_cache."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from articles_to_anki.cards.clusters import MissingApiKeyError
from articles_to_anki.enrichment.clusters import analyse_cluster_assignment
from articles_to_anki.enrichment.contexts import approve_context_candidates
from articles_to_anki.models import ClusterAnalysis, ClusterCandidate, ContextCandidate


def _llm_cache_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "caches"


def _read_cache_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_cache_file(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _candidate_cache_payload(candidate: Any) -> dict[str, Any]:
    """Stable content of one cluster candidate for cache keys (model or dict)."""
    cluster_id = getattr(candidate, "cluster_id", None)
    leader = getattr(candidate, "leader", None)
    examples = getattr(candidate, "examples", None)
    if cluster_id is None and isinstance(candidate, dict):
        cluster_id = candidate.get("cluster_id")
        leader = candidate.get("leader")
        examples = candidate.get("examples")
    example_payload = []
    for item in examples or []:
        highlight = getattr(item, "highlight", None)
        context = getattr(item, "context", None)
        if highlight is None and isinstance(item, dict):
            highlight = item.get("highlight")
            context = item.get("context")
        example_payload.append(
            {"highlight": str(highlight or ""), "context": str(context or "")}
        )
    return {"cluster_id": str(cluster_id or ""), "leader": str(leader or ""), "examples": example_payload}


def cluster_analysis_cached(
    cache_dir: Path,
    user_id: int,
    *,
    model: str,
    target: str,
    normalized_target: str,
    sentence: str,
    candidates: list[Any],
    api_key: str | None,
) -> tuple[ClusterAnalysis, bool]:
    """Analyse one highlight into a semantic cluster, reusing cached results.

    The cache is keyed by the exact model inputs, so repeated enrichments and
    deck rebuilds never pay for the same LLM call twice. Returns the analysis
    and whether it came from the cache.
    """
    if any(isinstance(candidate, dict) for candidate in candidates):
        candidates = [
            ClusterCandidate.model_validate(candidate) for candidate in candidates
        ]
    key_payload = json.dumps(
        {
            "model": model,
            "target": target,
            "normalized_target": normalized_target,
            "sentence": sentence,
            "candidates": [
                _candidate_cache_payload(candidate) for candidate in candidates
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
    path = cache_dir / f"cluster-analysis-{user_id}.json"
    cache = _read_cache_file(path)
    cached = cache.get(key)
    if cached is not None:
        try:
            return ClusterAnalysis.model_validate(cached), True
        except Exception:
            pass
    if not api_key:
        raise MissingApiKeyError
    analysis = analyse_cluster_assignment(
        target,
        normalized_target,
        sentence,
        candidates,
        api_key=api_key,
        model=model,
    )
    cache = _read_cache_file(path)
    cache[key] = analysis.model_dump()
    _write_cache_file(path, cache)
    return analysis, False


def context_approval_cached(
    cache_dir: Path,
    user_id: int,
    *,
    model: str,
    leader: str,
    definition: str,
    translations: list[str],
    known_contexts: list[str],
    candidates: list[ContextCandidate],
    api_key: str | None,
) -> set[str] | None:
    """Approve mined article contexts, reusing cached decisions by content."""
    key_payload = json.dumps(
        {
            "model": model,
            "leader": leader,
            "definition": definition,
            "translations": translations,
            "known_contexts": known_contexts,
            "candidates": [
                {"id": item.id, "surface": item.surface, "sentence": item.sentence}
                for item in candidates
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
    path = cache_dir / f"context-approval-{user_id}.json"
    cache = _read_cache_file(path)
    cached = cache.get(key)
    if cached is not None:
        return set(cached)
    if not api_key:
        return None
    approved = approve_context_candidates(
        leader=leader,
        definition=definition,
        translations=translations,
        known_contexts=known_contexts,
        candidates=candidates,
        api_key=api_key,
        model=model,
    )
    cache = _read_cache_file(path)
    cache[key] = sorted(approved)
    _write_cache_file(path, cache)
    return approved
