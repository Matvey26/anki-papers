"""Compatibility API for enrichment clients."""
from .enrichment.client import (
    _apply_model_generation_config as _apply_model_generation_config,
)
from .enrichment.client import _post_json as _post_json
from .enrichment.client import _strip_json_code_fence as _strip_json_code_fence
from .enrichment.clusters import (
    analyse_cluster_assignment as analyse_cluster_assignment,
)
from .enrichment.contexts import (
    approve_context_candidates as approve_context_candidates,
)
from .enrichment.prompts import CACHE_VERSION as CACHE_VERSION
from .enrichment.prompts import CLUSTER_SYSTEM_PROMPT as CLUSTER_SYSTEM_PROMPT
from .enrichment.prompts import (
    CONTEXT_APPROVAL_SYSTEM_PROMPT as CONTEXT_APPROVAL_SYSTEM_PROMPT,
)
from .enrichment.prompts import DEEPSEEK_MODEL_PREFIX as DEEPSEEK_MODEL_PREFIX
from .enrichment.prompts import DEFAULT_MODEL as DEFAULT_MODEL
from .enrichment.prompts import DEFAULT_SEMANTIC_MODEL as DEFAULT_SEMANTIC_MODEL
from .enrichment.prompts import MAX_APPROVAL_ATTEMPTS as MAX_APPROVAL_ATTEMPTS
from .enrichment.prompts import OPENROUTER_URL as OPENROUTER_URL
from .enrichment.prompts import RETRY_INSTRUCTIONS as RETRY_INSTRUCTIONS
from .enrichment.prompts import RETRY_TEMPERATURES as RETRY_TEMPERATURES
from .enrichment.prompts import SYSTEM_PROMPT as SYSTEM_PROMPT
from .enrichment.targets import _cache_key as _cache_key
from .enrichment.targets import _letters_only as _letters_only
from .enrichment.targets import _read_cache as _read_cache
from .enrichment.targets import _request_batch as _request_batch
from .enrichment.targets import (
    _validate_recall_distractors as _validate_recall_distractors,
)
from .enrichment.targets import _write_cache as _write_cache
from .enrichment.targets import build_openrouter_payload as build_openrouter_payload
from .enrichment.targets import enrich_targets as enrich_targets
from .enrichment.targets import load_env_file as load_env_file
