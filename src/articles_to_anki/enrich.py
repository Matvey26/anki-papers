from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import (
    ClusterAnalysis,
    ClusterCandidate,
    ContextApprovalBatch,
    ContextCandidate,
    EnrichedItem,
    EnrichmentBatch,
    EnrichmentRequestItem,
    RecallDistractors,
    TargetContext,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEEPSEEK_MODEL_PREFIX = "deepseek/deepseek-v4-flash-0731"
DEFAULT_MODEL = f"{DEEPSEEK_MODEL_PREFIX}:nitro"
DEFAULT_SEMANTIC_MODEL = DEFAULT_MODEL
CACHE_VERSION = "v5"
MAX_APPROVAL_ATTEMPTS = 3
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
3. Start translations_ru with the single best Russian answer for this occurrence. Add up to four
   UNIQUE close variants only when each deserves the same score as a learner's answer in this
   exact sentence. A related concept that merely preserves the sentence's general message is not
   the same lexical meaning. Reject hypernyms, hyponyms, prerequisites, consequences, paraphrases,
   and words with weaker or stronger commitment even when the full sentence remains plausible.
   Quality is more important than quantity: one precise translation is a complete answer. Never
   add a weaker translation or another dictionary sense to fill a list.
4. Give replacement_ru in the exact Russian grammatical form that can replace the English
   target inside the otherwise English sentence. Preserve tense, number, and discourse role.
   Mentally substitute it into the full sentence and verify that the sentence's intended
   meaning remains intact. Do not add a comma, period, colon, semicolon, exclamation mark, or
   question mark after replacement_ru: punctuation adjacent to the target is already preserved.
5. forbidden_alternatives_en is NOT a thesaurus or association list. Start it as an empty list.
   Consider at most 6 simpler English near-synonyms that a learner might use as the card answer.
   Add a candidate only after literally replacing the exact target span with it while keeping
   every other character of the sentence unchanged. The resulting sentence must be natural and
   grammatical, preserve the target's syntactic role and collocation, and state substantially the
   SAME claim. Reject a candidate if a native editor would need to change any neighboring
   preposition, object, article, agreement, punctuation, or word order. A dictionary synonym is
   invalid when it fits only after such an edit. For fixed constructions and strong collocations,
   an empty list is normal and preferable to approximate alternatives.
   NEVER give antonyms, opposites, unrelated words, Russian words, the target itself, or trivial
   spelling/case variants. Reject candidates that negate, reverse, weaken, strengthen, broaden,
   or narrow the original claim merely because they are topically related.
6. Treat a multiword target as one expression.
7. context_explanation_ru, every item in translations_ru, and replacement_ru MUST be written
   in Russian Cyrillic. Never leave the English target in replacement_ru.
The object for each item MUST contain exactly these five property names: id,
context_explanation_ru, translations_ru, replacement_ru, forbidden_alternatives_en. Never
rename any property. The near-synonym list is ALWAYS stored under the literal property name
forbidden_alternatives_en; never rename it to near_synonyms or anything else.
Return every input id once and only once. Follow the supplied JSON Schema exactly.
"""

CLUSTER_SYSTEM_PROMPT = """\
Assign ONE highlighted English word or expression to a learnable word-formation cluster and build
its English-to-Russian card context.

You receive the raw highlight, its normalized form, its full sentence, and zero to five candidate
clusters found only by fuzzy spelling similarity. Each candidate has a stable cluster_id, a leader,
and up to five real highlights with their contexts. Fuzzy similarity is retrieval only: it is not
evidence that meanings or word families match.

Set cluster_id to exactly one supplied candidate cluster_id only when the new highlight is
word-formation-related to that cluster AND shares its central learnable meaning. Inflections and
derivations may share a cluster: acquire/acquires/acquired/acquisition can use leader acquire.
Homonyms with identical spelling but unrelated meanings must remain separate clusters: train as a
noun meaning a railway vehicle and train as a verb meaning practise need different cluster_id
values. Different senses that would teach different answers also need separate clusters.

Set cluster_id to the literal value new_cluster when no candidate qualifies. This is always a valid
choice, even when candidates exist. Never invent a cluster_id. For new_cluster, leader must be the
canonical lowercase representative: a base word, normalized phrase, or fixed expression. For an
existing cluster, repeat its supplied leader exactly. Fixed expressions that cannot be usefully
re-formed remain singleton clusters, e.g. it's worth has leader it's worth.

cluster_definition_en must precisely cover the selected cluster after adding this occurrence. For a
new cluster it defines this occurrence's sense. For an existing cluster it is a concise umbrella
definition that covers both old examples and the new occurrence without admitting unrelated senses.

Give 1-4 precise Russian translations of the highlighted span. replacement_ru must translate only
that span, preserving its contextual case, number, tense, and role without absorbing neighboring
words. Never invent sources or statistics.

Build source_distractors in two stages. First brainstorm:
- substitutes_en: as many defensible words or compact phrases as the schema allows that can replace
  only the highlighted surface while all surrounding text stays frozen. The result must read as a
  natural, meaningful sentence; a meaning shift or slight style mismatch is allowed.
- related_en: as many close semantic neighbors as possible, whether or not exact insertion works.
  Exclude distant associations, antonyms, and words needing major semantic qualifications.
Then judge the visible subsets conservatively:
- valid_substitutes_en: only high-confidence items from substitutes_en that pass literal insertion.
- valid_related_en: only high-confidence items from related_en that are closest in meaning,
  regardless of whether literal insertion works. Overlap between valid lists is allowed.
Before adding any substitute, silently read the full literal result. In a fixed frame or strong
collocation, accept only words established with the same neighbors; reject any candidate needing a
different verb, preposition, determiner, or word order. Never repair the sentence or test a
different construction. Exclude the target and spelling/case variants, Russian words,
explanations, and sentences. Valid lists may be empty; quality beats count.
Return only JSON matching the schema.
"""

CONTEXT_APPROVAL_SYSTEM_PROMPT = """\
You decide which real sentences from the user's articles may be added as extra learning contexts
to an existing English vocabulary card.

The card trains ONE lexical sense of the word family:
- leader: {leader}
- meaning: {definition}
- Russian translations: {translations_ru}
- already known contexts: {known_contexts}

Each candidate sentence contains the surface form in angle brackets, e.g. <impaired>. Surfaces
are found only by spelling/word-formation similarity, NOT by meaning, so any of them may be a
different word, a homonym, a different sense, or a proper noun.

Set suitable=true only when ALL of these hold:
1. The surface in THIS sentence expresses exactly the sense the card trains. A different sense of
   the same spelling, a homonym, a fixed expression with its own meaning, or a proper noun is NOT
   suitable, even inside a topically related sentence.
2. The sentence is natural, self-contained, and understandable without extra context.
3. The sentence would genuinely help an English learner recall this exact meaning.

Return one object with properties id and suitable for EVERY candidate id, exactly once.
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
    result = _post_json(payload, api_key)
    content = result["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise RuntimeError("OpenRouter returned non-text cluster analysis.")
    analysis = ClusterAnalysis.model_validate_json(_strip_json_code_fence(content))
    if analysis.cluster_id not in allowed_cluster_ids:
        raise RuntimeError("OpenRouter selected a cluster outside the candidate set.")
    _validate_recall_distractors(target, analysis.source_distractors)
    return analysis


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
            result = _post_json(payload, api_key)
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


def _strip_json_code_fence(content: str) -> str:
    stripped = content.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return match.group(1) if match else stripped


def _apply_model_generation_config(
    payload: dict[str, Any],
    *,
    model: str,
    temperature: float,
) -> None:
    if not model.startswith("openai/gpt-5.6-luna"):
        payload["temperature"] = temperature
    if model.startswith(DEEPSEEK_MODEL_PREFIX):
        payload["reasoning"] = {"effort": "none"}


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
