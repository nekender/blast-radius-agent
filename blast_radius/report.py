"""Stage 6 · Write the report for a QA lead, then check it.

The LLM never sees the code or the graph, only the BlastRadius facts, and it returns structured
sections where every bullet cites the ids of the facts it is about. Code then checks:
  - every cited id exists in the facts (no invented screens, flows or requirements)
  - every high-confidence impact is mentioned somewhere (nothing important silently dropped)
One rewrite with the errors fed back; if it still fails, the deterministic template report is used.
"""
from __future__ import annotations

from pydantic import BaseModel

from . import llm
from .config import SETTINGS
from .models import BlastRadius

KIND_TITLE = {"requirement": "Requirements affected", "flow": "User flows affected", "test": "Tests to re-run",
              "element": "Screens and controls at risk", "screen": "Pages", "unmapped_code": "Changes we could not see in the app"}


def _level(c: float) -> str:
    return "High" if c >= SETTINGS.link_accept else "Medium" if c >= SETTINGS.link_review else "Low"


def template_report(br: BlastRadius) -> str:
    lines = [f"# What PR #{br.pr} could break", "", f"**{br.title}** · [{br.url}]({br.url})", ""]
    reqs = [i for i in br.impacts if i.kind == "requirement"]
    flows = [i for i in br.impacts if i.kind == "flow"]
    els = [i for i in br.impacts if i.kind == "element"]
    lines += ["## In one line",
              f"This change touches {len(els)} control(s) across {len({i.label for i in br.impacts if i.kind == 'screen'})} page(s), "
              f"{len(flows)} user flow(s) and {len(reqs)} requirement(s). "
              f"{len(br.needs_review)} item(s) need a person to check.", ""]
    for kind in ("flow", "test", "requirement", "element", "unmapped_code"):
        items = [i for i in br.impacts if i.kind == kind]
        if not items:
            continue
        lines += [f"## {KIND_TITLE[kind]}", "", "| Confidence | What | Why we think so |", "|---|---|---|"]
        for i in items:
            seen = "" if i.observed else " *(not seen in the running app)*"
            lines.append(f"| {_level(i.confidence)} ({i.confidence:.2f}) | {i.label}{seen} | {'; '.join(i.why)} |")
        lines.append("")
    if br.coverage_lost:
        lines += ["## Requirements that lose test coverage", ""]
        lines += [f"- **{c['statement']}**: {c['why']}" for c in br.coverage_lost] + [""]
    if br.needs_review:
        lines += ["## Needs a person", ""]
        lines += [f"- {n['item']}: {n['why']}" for n in br.needs_review] + [""]
    lines += ["## Code that changed", ""]
    lines += [f"- `{c['file']}` · {c['unit']}" for c in br.changed]
    lines += _assessment_md(br) + _cost_md(br)
    return "\n".join(lines) + "\n"


def _cost_md(br: BlastRadius) -> list[str]:
    c = br.cost or {}
    t = (c.get("llm") or {}).get("total")
    if not t:
        return []
    stages = c.get("seconds_by_stage", {})
    by = (c.get("llm") or {}).get("by_stage", {})
    rows = " · ".join(f"{k} ${v['usd_uncached']:.3f}" for k, v in by.items())
    return ["", "## What this report cost", "",
            f"- **${t['usd_uncached']:.3f}** from scratch ({t['api_calls'] or t['calls']} model calls, "
            f"{t['input_tokens']:,} input / {t['output_tokens']:,} output tokens, {c['llm']['model']}); "
            f"this run paid ${t['usd']:.3f} after caching. By stage: {rows}.",
            f"- {c.get('seconds_total', 0)} s wall clock: " + ", ".join(f"{k} {v} s" for k, v in stages.items()) + "."]


def _assessment_md(br: BlastRadius) -> list[str]:
    a = br.assessment or {}
    if not a.get("applied") and not a.get("rejected"):
        return []
    label = {i.id: i.label for i in br.impacts}
    out = ["", "## How the assessment agent changed the ranking", "",
           "| Item | Before | After | Why |", "|---|---|---|---|"]
    out += [f"| {label.get(x['id'], x['id'])} | {x['from']:.2f} | {x['to']:.2f}{' (capped)' if x.get('capped') else ''} "
            f"| {x['reason']} |" for x in a.get("applied", []) if abs(x["to"] - x["from"]) >= 0.005]
    out += [f"| {label.get(x['id'], x['id'])} | | rejected | {x['rejected_because']} |" for x in a.get("rejected", [])]
    out += [f"", f"*{a.get('tool_calls', 0)} tool calls; full trace in `reports/pr-{br.pr}.json`.*"]
    return out


class Bullet(BaseModel):
    text: str
    refs: list[str]


class Section(BaseModel):
    title: str
    bullets: list[Bullet]


class Written(BaseModel):
    headline: str
    what_to_retest: list[Bullet]
    sections: list[Section]


SYSTEM = """You write a change-impact note for a QA lead who knows the product but not the code.
You get facts about a pull request: what changed, which flows, pages, controls and requirements it affects,
each with an id, a confidence and the evidence. Rules:
- Plain product language: say "the Publish Article button on the editor page", not method or file names.
- Tests are named by their test name; group tests that act on the same control in one bullet (cite all their ids).
- Every bullet lists in refs the ids of the facts it is about. Use only ids from the facts.
- Mention every fact with confidence >= 0.75. Say clearly which items were never seen in the running app; if every
  item was seen, don't write a section about it. Never write a section or bullet that has nothing to report.
- If what_the_change_does is given, open with it in plain words. Re-test items in the order of the impact list.
- No numbers or claims that are not in the facts. Short sentences."""


def _facts(br: BlastRadius) -> dict:
    return {"pr": br.pr, "title": br.title,
            "what_the_change_does": (br.assessment or {}).get("behaviour_change", ""),
            "impacts": [{"id": i.id, "kind": i.kind, "label": i.label, "confidence": i.confidence,
                         "observed_in_app": i.observed, "evidence": i.why} for i in br.impacts if i.kind != "screen"],
            "coverage_lost": br.coverage_lost, "needs_review": br.needs_review}


def verify(w: Written, br: BlastRadius) -> list[str]:
    ids = {i.id for i in br.impacts}
    cited = {r for b in w.what_to_retest for r in b.refs} | {r for s in w.sections for b in s.bullets for r in b.refs}
    errors = [f"cited unknown id {r}" for r in sorted(cited - ids)]
    must = {i.id for i in br.impacts if i.confidence >= SETTINGS.link_accept and i.kind != "screen"}
    errors += [f"high-confidence item not mentioned: {i}" for i in sorted(must - cited)]
    return errors


def render(w: Written, br: BlastRadius) -> str:
    conf = {i.id: i for i in br.impacts}

    def tag(b: Bullet) -> str:
        levels = sorted({_level(conf[r].confidence) for r in b.refs if r in conf})
        unseen = any(not conf[r].observed for r in b.refs if r in conf)
        return f" *({'/'.join(levels)} confidence{', not seen in the app' if unseen else ''})*" if levels else ""

    out = [f"# What PR #{br.pr} could break", "", f"**{br.title}** · [{br.url}]({br.url})", "", f"> {w.headline}", "",
           "## Re-test first", ""]
    out += [f"{n}. {b.text}{tag(b)}" for n, b in enumerate(w.what_to_retest, 1)] + [""]
    for s in w.sections:
        out += [f"## {s.title}", ""] + [f"- {b.text}{tag(b)}" for b in s.bullets] + [""]
    out += ["## Code that changed", ""] + [f"- `{c['file']}` · {c['unit']}" for c in br.changed]
    out += _assessment_md(br) + _cost_md(br)
    return "\n".join(out) + "\n"


def write(br: BlastRadius, use_cache: bool = True) -> tuple[str, dict]:
    """Returns (markdown, meta). meta says which writer produced it and any verification errors."""
    if not llm.available():
        return template_report(br), {"writer": "template", "errors": ["no ANTHROPIC_API_KEY"]}
    import json
    prompt = json.dumps(_facts(br), indent=1)
    errors: list[str] = []
    for attempt in range(2):
        p = prompt if not errors else f"{prompt}\n\nFix these problems from your last draft: {errors}"
        w = llm.structured(SYSTEM, p, Written, use_cache=use_cache, max_tokens=8000)
        errors = verify(w, br)
        if not errors:
            return render(w, br), {"writer": "llm", "attempts": attempt + 1, "errors": []}
    return template_report(br), {"writer": "template-fallback", "errors": errors}
