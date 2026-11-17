"""The only place the LLM is called.

- Output is always structured: the API's structured-output mode, constrained to a Pydantic model's JSON schema.
- Responses are cached on disk by (model, prompt, schema) so a re-run is reproducible and free.
  Evals pass use_cache=False to measure run-to-run variance.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import SETTINGS

T = TypeVar("T", bound=BaseModel)
_client = None


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def structured(system: str, prompt: str, schema: type[T], *, use_cache: bool = True,
               max_tokens: int = 4000) -> T:
    schema_json = schema.model_json_schema()
    key = hashlib.sha256(json.dumps([SETTINGS.model, system, prompt, schema_json], sort_keys=True).encode()).hexdigest()
    cache = SETTINGS.cache_dir / "llm" / f"{key}.json"
    if use_cache and cache.exists():
        return schema.model_validate_json(cache.read_text())

    last_error = None
    for _ in range(2):  # one retry, with the validation error fed back
        user = prompt if last_error is None else f"{prompt}\n\nYour previous answer failed validation: {last_error}. Fix it."
        try:
            resp = _get_client().messages.parse(
                model=SETTINGS.model, max_tokens=max_tokens, system=system, output_format=schema,
                messages=[{"role": "user", "content": user}],
            )
            out = resp.parsed_output
            if out is None:
                raise ValueError(f"no structured output (stop_reason={resp.stop_reason})")
        except (ValidationError, ValueError) as e:
            last_error = str(e)[:800]
            continue
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(out.model_dump_json())
        return out
    raise RuntimeError(f"LLM output failed validation twice: {last_error}")
