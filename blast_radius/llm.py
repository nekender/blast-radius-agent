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


# ---------------- tool-using agent loop ----------------

class Tool:
    """A tool the model may call. `fn(**input) -> str` runs in our code; the model only sees its text result."""

    def __init__(self, name: str, description: str, params: dict, fn):
        self.name, self.description, self.params, self.fn = name, description, params, fn

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object", "properties": self.params, "required": list(self.params)}}


def agent_loop(system: str, prompt: str, tools: list[Tool], finish: str, *, max_turns: int = 15,
               max_tokens: int = 4000, on_step=None) -> tuple[dict | None, list[dict]]:
    """Run the model with tools until it calls the `finish` tool or runs out of turns.

    Returns (finish tool input or None, trace). The trace records every call and result, so a run can be
    replayed, inspected and evaluated (trajectory, not just the final answer). The loop is the only place
    the model acts; every action is a tool that our code implements and can refuse.
    """
    by_name = {t.name: t for t in tools}
    messages = [{"role": "user", "content": prompt}]
    trace: list[dict] = []
    for turn in range(max_turns):
        resp = _get_client().messages.create(model=SETTINGS.model, max_tokens=max_tokens, system=system,
                                             tools=[t.spec() for t in tools], messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            messages.append({"role": "user", "content": f"Use a tool. Call `{finish}` when you are done."})
            continue
        results = []
        for c in calls:
            if c.name == finish:
                trace.append({"turn": turn, "tool": c.name, "input": c.input})
                return c.input, trace
            try:
                out = by_name[c.name].fn(**c.input) if c.name in by_name else f"error: no tool named {c.name}"
            except Exception as e:  # noqa: BLE001 - a failed tool is information for the model, not a crash
                out = f"error: {e.__class__.__name__}: {e}"
            out = str(out)[:6000]
            trace.append({"turn": turn, "tool": c.name, "input": c.input, "output": out[:500]})
            if on_step:
                on_step(trace[-1])
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": out})
        messages.append({"role": "user", "content": results})
    return None, trace
