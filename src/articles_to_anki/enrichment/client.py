"""enrichment / client."""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

from articles_to_anki.enrichment.prompts import DEEPSEEK_MODEL_PREFIX, OPENROUTER_URL


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
