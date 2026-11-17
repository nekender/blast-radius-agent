"""The only place the LLM is called.

- Output is always structured: the API's structured-output mode, constrained to a Pydantic model's JSON schema.
- Responses are cached on disk by (model, prompt, schema) so a re-run is reproducible and free.
  Evals pass use_cache=False to measure run-to-run variance.
- Every API call is recorded in a cost ledger (stage, tokens, dollars, seconds). Cache hits are recorded too,
  at $0 and with the tokens the original call used, so "cost on a warm cache" and "cost from scratch" are both known.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import SETTINGS

T = TypeVar("T", bound=BaseModel)
_client = None

# ---------------- cost ledger ----------------

# $ per million tokens: (input, output, cache read). Anthropic first-party rates, from the Claude API reference
# (cached 2026-10-06). Thinking tokens are billed as output and are included in output_tokens.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
LEDGER: list[dict] = []
_stage = contextvars.ContextVar("stage", default="other")


@contextlib.contextmanager
def stage(name: str):
    """Attribute every model call inside the block to `name` (e.g. 'assess', 'report', 'spec')."""
    token = _stage.set(name)
    try:
        yield
    finally:
        _stage.reset(token)


def _usage(resp) -> dict:
    u = resp.usage
    return {"input": u.input_tokens or 0, "output": u.output_tokens or 0,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}


def dollars(u: dict, model: str = "") -> float:
    pin, pout, pread = PRICES.get(model or SETTINGS.model, (0.0, 0.0, 0.0))
    # cache writes bill at 1.25x input on the 5-minute cache; this project sends no cache_control, so it's 0 here
    return (u["input"] * pin + u["output"] * pout + u["cache_read"] * pread + u["cache_write"] * pin * 1.25) / 1e6


def _record(u: dict, seconds: float, cached: bool, kind: str) -> None:
    LEDGER.append({"stage": _stage.get(), "kind": kind, "model": SETTINGS.model, "cached": cached,
                   "seconds": round(seconds, 2), **u, "usd": 0.0 if cached else round(dollars(u), 6),
                   "usd_uncached": round(dollars(u), 6)})


def summarize(entries: list[dict] | None = None) -> dict:
    """Totals and a per-stage breakdown. 'usd' is what this run paid; 'usd_uncached' what it costs from scratch."""
    entries = LEDGER if entries is None else entries
    def tot(es):
        return {"calls": len(es), "api_calls": sum(not e["cached"] for e in es),
                "input_tokens": sum(e["input"] for e in es), "output_tokens": sum(e["output"] for e in es),
                "usd": round(sum(e["usd"] for e in es), 4), "usd_uncached": round(sum(e["usd_uncached"] for e in es), 4),
                "llm_seconds": round(sum(e["seconds"] for e in es if not e["cached"]), 1)}
    stages = sorted({e["stage"] for e in entries})
    return {"model": SETTINGS.model, "total": tot(entries),
            "by_stage": {s: tot([e for e in entries if e["stage"] == s]) for s in stages}}


def reset_ledger() -> list[dict]:
    old = LEDGER[:]
    LEDGER.clear()
    return old


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
        raw = json.loads(cache.read_text())
        if isinstance(raw, dict) and "__output__" in raw:
            _record(raw["__usage__"], 0.0, True, "structured")
            return schema.model_validate(raw["__output__"])
        return schema.model_validate(raw)   # cache written before the ledger existed (usage unknown)

    last_error = None
    for _ in range(2):  # one retry, with the validation error fed back
        user = prompt if last_error is None else f"{prompt}\n\nYour previous answer failed validation: {last_error}. Fix it."
        try:
            t0 = time.monotonic()
            resp = _get_client().messages.parse(
                model=SETTINGS.model, max_tokens=max_tokens, system=system, output_format=schema,
                messages=[{"role": "user", "content": user}],
            )
            usage = _usage(resp)
            _record(usage, time.monotonic() - t0, False, "structured")
            out = resp.parsed_output
            if out is None:
                raise ValueError(f"no structured output (stop_reason={resp.stop_reason})")
        except (ValidationError, ValueError) as e:
            last_error = str(e)[:800]
            continue
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"__output__": out.model_dump(mode="json"), "__usage__": usage}))
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
        t0 = time.monotonic()
        resp = _get_client().messages.create(model=SETTINGS.model, max_tokens=max_tokens, system=system,
                                             tools=[t.spec() for t in tools], messages=messages)
        _record(_usage(resp), time.monotonic() - t0, False, "agent_turn")
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
