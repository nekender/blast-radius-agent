"""Stage 5b · Assess the blast radius (LLM agent with graph tools, verified).

The deterministic traversal says WHAT is connected to the changed code. It can't say what the change DOES:
PR #311 touches `submitForm`, so every flow through the Publish button is flagged, including journeys that
only use it as a setup step. This agent reads the diff, inspects the code and the graph through tools, and
proposes confidence adjustments with reasons.

  model decides   what to inspect, what the change does to behaviour, which impacts to raise or lower
  code enforces   it can only adjust impacts that exist; it must have inspected an item (via a tool) before
                  adjusting it; changes are bounded (floor 0.3, at most 0.5 per assessment, capped not
                  vetoed; only observed items may go above 0.9); nothing is ever removed, so the deterministic result stays the safety floor
  code checks     adjustments to items never inspected are sent back once ("inspect these, resubmit");
                  whatever is still ungrounded after that is rejected
  code records    the full tool trace, every applied and rejected adjustment, with the reason
"""
from __future__ import annotations

import hashlib
import json

from . import llm
from .code_map import read_at
from .config import SETTINGS
from .graph import Graph
from .models import BlastRadius, CodeMap

SYSTEM = """You are a senior QA engineer assessing a pull request's blast radius. A graph traversal has listed
the pages, controls, user flows and requirements connected to the changed code, each with a confidence.
Connection is not the same as risk. Your job: work out what the change actually does to behaviour, then
adjust confidences so the QA lead re-tests what matters first.

Use the tools: read the diff, read the changed method, inspect the flows, tests, controls and requirements you want
to adjust. Typical reasons to lower: the item only uses the changed code in a setup step, or the changed lines
don't affect the branch it exercises. Typical reasons to raise: the item exercises exactly the changed branch.
You can only adjust items you have inspected (inspect takes a list, so inspect everything you intend to adjust,
in one or two calls). Never remove anything. Then call finish."""


def _inspected(trace: list[dict], ids: set[str]) -> set[str]:
    """What the agent actually looked at, read from its trace: the single source for the grounding check."""
    return {i for t in trace if t["tool"] == "inspect" for i in t["input"].get("item_ids", [])} & ids


def _facts(br: BlastRadius) -> list[dict]:
    return [{"id": i.id, "kind": i.kind, "label": i.label, "confidence": i.confidence, "observed": i.observed}
            for i in br.impacts if i.kind != "screen"]


def assess(br: BlastRadius, pr: dict, head: CodeMap, graph: Graph, use_cache: bool = True,
           max_turns: int = 12) -> BlastRadius:
    if not llm.available():
        br.assessment = {"skipped": "no ANTHROPIC_API_KEY"}
        return br
    key = hashlib.sha256(json.dumps([SETTINGS.model, SYSTEM, pr["files"], _facts(br)], sort_keys=True).encode()).hexdigest()
    cache = SETTINGS.cache_dir / "assess" / f"{key}.json"
    inspected: set[str] = set()
    ids = {i.id for i in br.impacts}

    def read_diff() -> str:
        return "\n\n".join(f"--- {f['path']}\n{f['patch']}" for f in pr["files"])

    def method_source(method_id: str) -> str:
        comp_name, _, m_name = method_id.partition(".")
        comp = next((c for c in head.components if c.name == comp_name), None)
        m = next((x for x in comp.methods if x.name == m_name), None) if comp else None
        if not m:
            return f"no method {method_id}; changed: {[c['resolved'] for c in br.changed]}"
        lines = (read_at(head.commit, comp.file) or "").splitlines()
        return "\n".join(f"{n}: {lines[n - 1]}" for n in range(m.start, min(m.end, len(lines)) + 1))

    def impacts() -> str:
        return json.dumps(_facts(br), indent=0)

    def inspect(item_ids: list[str]) -> str:
        return "\n\n".join(f"[{i}]\n{_inspect_one(i)}" for i in item_ids[:40])

    def _inspect_one(item_id: str) -> str:
        if item_id not in ids:
            return f"error: {item_id} is not in the impact list"
        imp = next(i for i in br.impacts if i.id == item_id)
        extra = {}
        if imp.kind == "flow":
            r = graph.run("MATCH (f:Flow {id: $id}) RETURN f.narrative AS steps, f.calls AS calls", id=item_id)
            extra = r[0] if r else {}
        elif imp.kind == "requirement":
            r = graph.run("MATCH (q:Requirement {id: $id}) OPTIONAL MATCH (q)-[c:COVERED_BY]->(x) "
                          "RETURN q.statement AS statement, collect(coalesce(x.label, x.id)) AS covered_by", id=item_id)
            extra = r[0] if r else {}
        elif imp.kind == "test":
            r = graph.run("MATCH (t:Test {id: $id}) OPTIONAL MATCH (t)-[u:USES]->(e:Element) "
                          "RETURN t.label AS test, collect(DISTINCT u.selector)[..12] AS steps_on_controls", id=item_id)
            extra = r[0] if r else {}
        elif imp.kind == "element":
            r = graph.run("MATCH (e:Element {id: $id}) OPTIONAL MATCH (e)-[:CALLS]->(ep) OPTIONAL MATCH (t)-[:RENDERS]->(e) "
                          "RETURN e.screen AS screen, collect(DISTINCT ep.id) AS calls, collect(DISTINCT t.id) AS rendered_by", id=item_id)
            extra = r[0] if r else {}
        return json.dumps({"label": imp.label, "confidence": imp.confidence, "observed": imp.observed,
                           "why": imp.why, **extra}, indent=0, default=str)

    if use_cache and cache.exists():
        saved = json.loads(cache.read_text())
        result, trace = saved["result"], saved["trace"]
        inspected = _inspected(trace, ids)
    else:
        tools = [
            llm.Tool("read_diff", "The PR's patch, per file.", {}, read_diff),
            llm.Tool("method_source", "Source of a changed method today, with line numbers, e.g. EditorComponent.submitForm.",
                     {"method_id": {"type": "string"}}, method_source),
            llm.Tool("impacts", "The current impact list (ids, labels, confidences).", {}, impacts),
            llm.Tool("inspect", "Details of up to 40 impacts at once: a flow's steps and calls, a requirement's evidence, "
                     "a control's calls. You must inspect an item before adjusting it.",
                     {"item_ids": {"type": "array", "items": {"type": "string"}}}, inspect),
            llm.Tool("finish", "Submit the assessment.", {
                "behaviour_change": {"type": "string", "description": "One or two sentences: what the change does to behaviour."},
                "adjustments": {"type": "array", "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "confidence": {"type": "number"}, "reason": {"type": "string"}},
                    "required": ["id", "confidence", "reason"]}},
                "retest_first": {"type": "array", "items": {"type": "string"}, "description": "Impact ids, most important first."},
            }, None),
        ]
        prompt = (f"PR #{br.pr}: {br.title}\nChanged code units: {[c['unit'] for c in br.changed]}\n\n"
                  f"Impact list:\n{impacts()}")
        result, trace = llm.agent_loop(SYSTEM, prompt, tools, "finish", max_turns=max_turns)
        inspected = _inspected(trace, ids)
        # Verifier feedback, once: adjustments to items it never inspected are sent back to inspect and resubmit.
        ungrounded = sorted({a["id"] for a in (result or {}).get("adjustments", []) if a["id"] in ids} - inspected)
        if ungrounded:
            retry = (f"{prompt}\n\nYour previous assessment was:\n{json.dumps(result)}\n\nThese adjustments were "
                     f"rejected because you never inspected the items: {ungrounded}. Inspect them, then call finish again "
                     f"with the complete assessment (all adjustments, not only these).")
            result2, trace2 = llm.agent_loop(SYSTEM, retry, tools, "finish", max_turns=max_turns)
            trace += [{"turn": "retry", "tool": "verifier", "input": {"ungrounded": ungrounded}}] + trace2
            result = result2 or result
            inspected = _inspected(trace, ids)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"result": result, "trace": trace}, default=str))

    applied, rejected = [], []
    for adj in (result or {}).get("adjustments", []):
        imp = next((i for i in br.impacts if i.id == adj["id"]), None)
        why = None
        if imp is None:
            why = "not an impact id"
        elif adj["id"] not in inspected:
            why = "adjusted without inspecting it"
        else:
            new = max(0.3, min(1.0, float(adj["confidence"])))
            if new > 0.9 and not imp.observed:
                why = "an unobserved item can't go above 0.9"
        if why:
            rejected.append({**adj, "rejected_because": why})
            continue
        old = imp.confidence
        clamped = abs(new - old) > 0.5      # one assessment moves an item at most 0.5: bounded, not vetoed
        if clamped:
            new = old + 0.5 if new > old else old - 0.5
        imp.confidence = round(max(0.3, new), 2)
        imp.why.append(f"assessment: {adj['reason']} ({old:.2f} -> {imp.confidence:.2f}{', capped' if clamped else ''})")
        applied.append({**adj, "from": old, "to": imp.confidence, "capped": clamped})
    order = {i: n for n, i in enumerate((result or {}).get("retest_first", [])) if i in ids}
    kinds = {"requirement": 0, "flow": 1, "test": 2, "element": 3, "screen": 4, "unmapped_code": 5}
    br.impacts.sort(key=lambda i: (kinds[i.kind], order.get(i.id, 99), -i.confidence))
    br.assessment = {"behaviour_change": (result or {}).get("behaviour_change", ""), "applied": applied,
                     "rejected": rejected, "retest_first": [i for i in order], "tool_calls": len(trace),
                     "finished": result is not None,
                     "trace": [{"tool": t["tool"], "input": t["input"]} for t in trace]}
    return br
