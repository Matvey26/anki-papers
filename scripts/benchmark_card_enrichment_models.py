from __future__ import annotations

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import articles_to_anki.enrich as enrich_module
from articles_to_anki.enrich import (
    build_openrouter_payload,
    enrich_targets,
    load_env_file,
)
from articles_to_anki.models import EnrichmentRequestItem
from articles_to_anki.webapp import make_target_context

MODELS = {
    "DeepSeek V4 Flash 0731": "deepseek/deepseek-v4-flash-0731",
    "Qwen3 30B A3B Instruct 2507": "qwen/qwen3-30b-a3b-instruct-2507",
    "Qwen3.5 Flash": "qwen/qwen3.5-flash-02-23",
    "Gemma 4 26B A4B": "google/gemma-4-26b-a4b-it",
    "Gemini 2.5 Flash-Lite": "google/gemini-2.5-flash-lite",
    "Gemma 4 31B": "google/gemma-4-31b-it",
}

CASES = [
    {
        "id": "error_endeavors",
        "kind": "known_error",
        "target": "endeavors",
        "sentence": (
            "Subsequently, we introduce our pre-training endeavors, including the "
            "training data construction, hyper-parameter settings, infrastructures, "
            "long context extension, and the evaluation of model performance and "
            "efficiency (Section 3)."
        ),
        "bad_example": "усилия по предварительному обучению",
    },
    {
        "id": "error_redundancy",
        "kind": "known_error",
        "target": "redundancy",
        "sentence": (
            "DeepSeekMoE has two key ideas: segmenting experts into finer granularity "
            "for higher expert specialization and more accurate knowledge acquisition, "
            "and isolating some shared experts for mitigating knowledge redundancy "
            "among routed experts."
        ),
        "bad_example": "избыточности знаний",
    },
    {
        "id": "error_magnitude",
        "kind": "known_error",
        "target": "magnitude",
        "sentence": (
            "They require a smaller magnitude of KV cache, but their performance does "
            "not match MHA (we provide the ablation of MHA, GQA and MQA in Appendix D.1)."
        ),
        "bad_example": "меньшего объёма",
    },
    {
        "id": "error_consideration",
        "kind": "known_error",
        "target": "consideration",
        "sentence": (
            "We take the load balance into consideration for automatically learned "
            "routing strategies."
        ),
        "bad_example": "во внимание",
    },
    {
        "id": "error_narrowing",
        "kind": "known_error",
        "target": "narrowing",
        "sentence": (
            "Its performance is comparable to leading closed-source models like GPT-4o "
            "and Claude-Sonnet-3.5, narrowing the gap between open-source and "
            "closed-source models in this domain."
        ),
        "bad_example": "сокращая разрыв",
    },
    {
        "id": "error_adjustment",
        "kind": "known_error",
        "target": "adjustment",
        "sentence": (
            "Through the dynamic adjustment, DeepSeek-V3 keeps balanced expert load "
            "during training, and achieves better performance than models that encourage "
            "load balance through pure auxiliary losses."
        ),
        "bad_example": "динамической корректировкой",
    },
    {
        "id": "error_lasting",
        "kind": "known_error",
        "target": "lasting",
        "sentence": (
            "As depicted in Figure 3, DeepSeek-LLM 1.3B, when trained on the DeepSeek-Math "
            "Corpus, shows a steeper learning curve along with more lasting improvements."
        ),
        "bad_example": "более долговременные улучшения",
    },
    {
        "id": "control_acknowledge",
        "kind": "regression_control",
        "target": "acknowledge",
        "sentence": (
            "While balance losses aim to encourage a balanced load, it is important to "
            "acknowledge that they cannot guarantee a strict load balance."
        ),
        "known_good": "признать",
    },
    {
        "id": "control_solely",
        "kind": "regression_control",
        "target": "solely",
        "sentence": (
            "Although the training is conducted solely at the sequence length of 32K, "
            "the model still demonstrates robust performance when being evaluated at a "
            "context length of 128K."
        ),
        "known_good": "исключительно",
    },
    {
        "id": "control_surpasses",
        "kind": "regression_control",
        "target": "surpasses",
        "sentence": (
            "While it trails behind GPT-4o and Claude-Sonnet-3.5 in English factual "
            "knowledge (SimpleQA), it surpasses these models in Chinese factual knowledge "
            "(Chinese SimpleQA), highlighting its strength in Chinese factual knowledge."
        ),
        "known_good": "превосходит",
    },
]


_transport = enrich_module._post_json
_transport_state = threading.local()


def _counted_transport(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    _transport_state.calls = getattr(_transport_state, "calls", 0) + 1
    return _transport(payload, api_key)


enrich_module._post_json = _counted_transport


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def run_model(
    model_name: str,
    model_id: str,
    *,
    api_key: str,
) -> dict[str, Any]:
    model_result: dict[str, Any] = {
        "model_name": model_name,
        "model_id": model_id,
        "cases": [],
    }
    for case in CASES:
        context = make_target_context(
            case["target"],
            case["sentence"],
            context_id=case["id"],
            page=1,
        )
        _transport_state.calls = 0
        started = time.monotonic()
        try:
            item = enrich_targets(
                [context],
                api_key=api_key,
                model=model_id,
                batch_size=1,
                cache_path=None,
                max_attempts=5,
            )[0]
        except Exception as exc:  # noqa: BLE001 - benchmark records provider failures
            result = {
                **case,
                "status": "error",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "api_calls": getattr(_transport_state, "calls", 0),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        else:
            result = {
                **case,
                "status": "success",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "api_calls": getattr(_transport_state, "calls", 0),
                "output": item.model_dump(),
            }
        model_result["cases"].append(result)
        print(
            f"{model_name}: {case['id']} -> {result['status']} "
            f"({result['elapsed_seconds']}s, {result['api_calls']} call(s))",
            flush=True,
        )
    return model_result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    load_env_file(args.env_file)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is missing")

    probe = build_openrouter_payload(
        [EnrichmentRequestItem(id="probe", target="probe", sentence="A probe.")],
        next(iter(MODELS.values())),
    )
    if probe.get("provider") != {"require_parameters": True}:
        raise RuntimeError("Production harness lost provider.require_parameters=true")

    report: dict[str, Any] = {
        "harness": {
            "function": "articles_to_anki.enrich.enrich_targets",
            "batch_size": 1,
            "max_attempts": 5,
            "max_tokens": probe["max_tokens"],
            "stream": probe["stream"],
            "temperature": probe.get("temperature"),
            "plugins": probe.get("plugins"),
            "response_format": probe["response_format"]["type"],
            "require_parameters": probe["provider"]["require_parameters"],
        },
        "models": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, report)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(run_model, name, model_id, api_key=api_key): name
            for name, model_id in MODELS.items()
        }
        for future in as_completed(futures):
            report["models"].append(future.result())
            report["models"].sort(key=lambda item: list(MODELS).index(item["model_name"]))
            write_json(args.output, report)

    print(args.output.resolve(), flush=True)


if __name__ == "__main__":
    main()
