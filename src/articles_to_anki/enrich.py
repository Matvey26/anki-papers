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
    EnrichedItem,
    EnrichmentBatch,
    EnrichmentRequestItem,
    RecallDistractors,
    SemanticAnalysis,
    SemanticCandidate,
    SemanticMatch,
    SemanticMatchResponse,
    TargetContext,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEEPSEEK_MODEL_PREFIX = "deepseek/deepseek-v4-flash-0731"
DEFAULT_MODEL = f"{DEEPSEEK_MODEL_PREFIX}:nitro"
DEFAULT_SEMANTIC_MODEL = DEFAULT_MODEL
CACHE_VERSION = "v5"
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

SEMANTIC_SYSTEM_PROMPT = """\
You are building high-quality English-to-Russian vocabulary cards for an advanced learner.
Analyse ONE highlighted target in its full sentence. Return its canonical English lemma, one
coarse part_of_speech (noun, verb, adjective, adverb, phrase, or other), and a short English
definition of exactly the sense used here. Also return family_key: the lowercase canonical base
of the learnable inflectional/derivational family. Examples: acquired and acquisition -> acquire;
acknowledged and acknowledging -> acknowledge; recognition and recognizable -> recognize.
Do not collapse unrelated words merely because they share spelling or an etymological root.
Treat phrasal verbs and fixed multiword expressions as whole families.

Give 1-4 Russian translations for this sense, and replacement_ru in grammatical form for this
specific source sentence. Then create exactly one NEW, realistic B2-or-harder academic context.
It must use a natural inflected surface form of the same lemma, be self-contained, be different
in syntax or collocation from the source sentence, and not claim to quote a real paper. Put that
exact form in generated_surface. Never invent citations, statistics, named studies, authors, or
URLs. generated_translation_ru must translate ONLY generated_surface, not the surrounding
sentence. It must be a compact Russian phrase in the exact grammatical form needed when inserted
into the otherwise English generated_sentence. Never translate, repeat, or summarize the full
generated_sentence in generated_translation_ru.

Build recall distractors separately for the source and generated contexts. Judge every candidate
with this exact procedure: copy the full sentence, replace only the highlighted surface, change
nothing else, then read the resulting literal sentence as a native editor would. Do not silently
repair a neighboring verb, preposition, article, determiner, agreement, punctuation, or word
order. Do not judge a candidate by imagining a different construction in which it could work.
- valid_substitutes_en has a strict entry test: the literal substituted sentence must already be
  natural and idiomatic as written, preserve the candidate's syntactic role and inflection, and
  express substantially the same contextual meaning. These are other compact English answers
  that genuinely fit, but are not the target answer this card is testing.
- meaning_related_non_substitutes_en contains tempting compact English answers from the same
  semantic neighborhood that fail that literal test because their grammatical form, syntactic
  role, required preposition, or collocation does not fit this exact sentence. A dictionary
  synonym that works only after editing surrounding text belongs here, not in valid_substitutes_en.
Put each candidate in exactly one category. Never include the context's target itself, spelling or
case variants, Russian words, antonyms, unrelated associations, explanations, or full sentences.
Start every list empty. Add only defensible candidates; there is no quota and an empty list is
valid. source_distractors must be judged against target and sentence. generated_distractors must
be judged independently against generated_surface and generated_sentence.
Return JSON only and follow the schema exactly.
"""

SEMANTIC_MATCH_PROMPT = """\
Decide whether the analysed source belongs on the same LEARNABLE CARD as one supplied candidate.
Candidates share either family_key or a conservative morphological prefix. Reject false lexical
relatives; spelling similarity alone is never enough.

Use same_sense for inflectional variants or effectively interchangeable meanings. Use
related_sense when the meanings share one clear central concept and seeing both contexts together
helps the learner acquire a broader, richer meaning. Derivational relatives and part-of-speech
changes may merge: acquire/acquired/acquisition and acknowledge/acknowledging are good examples
when their contexts preserve the same semantic core.

Use new_card when combining would teach two answers rather than enrich one concept. Polysemy must
stay separate: recognize meaning "identify from prior knowledge" is different from recognize
meaning "admit or acknowledge as true"; run physically, run a company, and run software are also
different cards. Similar spelling, family_key, or topic never overrides this rule.

For same_sense or related_sense, return the candidate card_id and a concise umbrella English
definition that covers both the existing candidate and source while excluding incompatible senses.
For new_card, card_id and merged_sense_definition_en must both be null. Explain briefly in Russian.
Return JSON only and follow the schema exactly.
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


def analyse_semantic_context(
    target: str,
    sentence: str,
    *,
    api_key: str,
    model: str = DEFAULT_SEMANTIC_MODEL,
) -> SemanticAnalysis:
    """Classify a lexical sense and create one explicitly synthetic extra context."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SEMANTIC_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"target": target, "sentence": sentence}, ensure_ascii=False)},
        ],
        "max_tokens": 900,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "semantic_context_analysis",
                "strict": True,
                "schema": SemanticAnalysis.model_json_schema(),
            },
        },
        "provider": {"require_parameters": True},
    }
    _apply_model_generation_config(payload, model=model, temperature=0.2)
    result = _post_json(payload, api_key)
    content = result["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise RuntimeError("OpenRouter returned non-text semantic analysis.")
    analysis = SemanticAnalysis.model_validate_json(content)
    if not _contains_exact_surface(
        analysis.generated_sentence,
        analysis.generated_surface,
    ):
        raise RuntimeError("Generated surface is absent from generated sentence.")
    _validate_recall_distractors(target, analysis.source_distractors)
    _validate_recall_distractors(
        analysis.generated_surface,
        analysis.generated_distractors,
    )
    return analysis


def _validate_recall_distractors(
    target: str,
    distractors: RecallDistractors,
) -> None:
    normalized_target = " ".join(target.casefold().split())
    values = (
        distractors.valid_substitutes_en
        + distractors.meaning_related_non_substitutes_en
    )
    if any(" ".join(value.casefold().split()) == normalized_target for value in values):
        raise RuntimeError("Recall distractors must not contain the target itself.")


def select_semantic_match(
    analysis: SemanticAnalysis,
    candidates: list[SemanticCandidate],
    *,
    api_key: str,
    model: str = DEFAULT_SEMANTIC_MODEL,
) -> SemanticMatch:
    """Choose a compatible semantic cluster inside one lexical family."""
    if not candidates:
        return SemanticMatch(
            card_id=None,
            relationship="new_card",
            merged_sense_definition_en=None,
            rationale_ru="В этом лексическом семействе пока нет карточек.",
        )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SEMANTIC_MATCH_PROMPT},
            {"role": "user", "content": json.dumps({"source": analysis.model_dump(), "candidates": [item.model_dump() for item in candidates]}, ensure_ascii=False)},
        ],
        "max_tokens": 500,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "semantic_match", "strict": True, "schema": SemanticMatchResponse.model_json_schema()},
        },
        "provider": {"require_parameters": True},
    }
    _apply_model_generation_config(payload, model=model, temperature=0.0)
    result = _post_json(payload, api_key)
    content = result["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise RuntimeError("OpenRouter returned non-text semantic match.")
    response = SemanticMatchResponse.model_validate_json(content)
    candidate_ids = {item.id for item in candidates}
    if response.match.card_id is not None and response.match.card_id not in candidate_ids:
        raise RuntimeError("OpenRouter selected a non-candidate semantic card.")
    return response.match


def _contains_exact_surface(sentence: str, surface: str) -> bool:
    return re.search(
        rf"(?<!\w){re.escape(surface)}(?!\w)",
        sentence,
        flags=re.IGNORECASE,
    ) is not None


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
