"""Stage 5 · Reason about a pull request (deterministic until the very last step).

1. Fetch the PR and its patch (cached under data/prs/ so runs are reproducible).
2. Map changed lines to code units at the PR's own commit: methods (by line range), template elements
   (by line), or "component-level" when a change sits outside any method (fields, decorator).
3. Carry units forward to the code the live app runs (HEAD) by component selector + method name,
   so moved or renamed files still resolve. Units that don't resolve are reported, not dropped.
4. Traverse the graph (Neo4j): units -> template elements -> UI elements -> screens -> flows -> requirements.
5. Score: confidence of a path = product of its edge confidences; an item keeps its best path.
6. Anything below the accept threshold, or reachable only in code (never observed in the UI), goes to
   "needs review" with the reason. The LLM only writes the prose afterwards (see report.py).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

import httpx

from . import code_map
from .config import SETTINGS
from .graph import Graph
from .models import BlastRadius, CodeMap, Impact

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def fetch_pr(number: int) -> dict:
    cache = SETTINGS.data / "prs" / f"{number}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    base = f"https://api.github.com/repos/{SETTINGS.repo}/pulls/{number}"
    with httpx.Client(headers={"Accept": "application/vnd.github+json"}, timeout=30) as c:
        pr = c.get(base).raise_for_status().json()
        files = c.get(f"{base}/files").raise_for_status().json()
    data = {"number": number, "title": pr["title"], "url": pr["html_url"], "body": pr.get("body") or "",
            "merge_commit": pr["merge_commit_sha"], "base": pr["base"]["sha"],
            "files": [{"path": f["filename"], "status": f["status"], "patch": f.get("patch", "")} for f in files]}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=2))
    return data


def changed_lines(patch: str) -> tuple[set[int], set[int]]:
    """(new-side lines added/modified, old-side lines removed)"""
    new, old = set(), set()
    o = n = 0
    for line in patch.splitlines():
        if (m := HUNK.match(line)):
            o, n = int(m.group(1)), int(m.group(3))
            continue
        if line.startswith("+"):
            new.add(n); n += 1
        elif line.startswith("-"):
            old.add(o); o += 1
        else:
            o += 1; n += 1
    return new, old


def _ensure_commit(sha: str) -> str:
    try:
        code_map.git("cat-file", "-e", sha)
    except Exception:  # noqa: BLE001
        code_map.git("fetch", "origin", sha)
    return sha


def units_for(cm: CodeMap, path: str, lines: set[int]) -> list[dict]:
    """Code units touched by `lines` of `path` in code map `cm`."""
    out = []
    if path.endswith(".html"):
        for t in cm.template_elements:
            if t.file == path and t.line in lines:
                out.append({"kind": "template", "component": t.component, "tag": t.tag, "text": t.text,
                            "name_attr": t.name_attr, "handlers": t.handlers, "line": t.line})
        return out
    for c in cm.components:
        if c.file != path:
            continue
        inside = set()
        for m in c.methods:
            hit = {l for l in lines if m.start <= l <= m.end}
            if hit:
                out.append({"kind": "method", "component": c.name, "selector": c.selector, "method": m.name,
                            "lines": sorted(hit)})
                inside |= hit
        if lines - inside:
            out.append({"kind": "component", "component": c.name, "selector": c.selector,
                        "lines": sorted(lines - inside)})
    return out


def follow_renames(path: str, since: str, head: CodeMap, max_hops: int = 5) -> list[str]:
    """Follow a file forward through deletions-that-were-renames (git rename detection at 20% similarity).
    Returns the chain of paths, ending at a file that exists in HEAD, or [] if the trail ends."""
    chain, current = [path], path
    head_files = {c.file for c in head.components}
    for _ in range(max_hops):
        if current in head_files:
            return chain
        out = code_map.git("log", "--diff-filter=D", "--format=%H", f"{since}..HEAD", "--", current).split()
        if not out:
            return []
        deleted_in = out[-1]  # earliest deletion after `since`
        renamed = [l.split("\t") for l in code_map.git("show", "-M20%", "--name-status", "--format=", deleted_in).splitlines()
                   if l.startswith("R") and l.split("\t")[1] == current]
        if not renamed:
            return []
        current = renamed[0][2]
        chain.append(f"{current} (renamed in {deleted_in[:7]}, {int(renamed[0][0][1:])}% similar)")
        since = deleted_in
        current = current
    return []


def resolve(unit: dict, head: CodeMap, since: str | None = None) -> tuple[list[str], str | None]:
    """Method ids in HEAD for a unit found at the PR's commit, plus a note when found through a rename."""
    comp = next((c for c in head.components if unit.get("selector") and c.selector == unit["selector"]), None) \
        or next((c for c in head.components if c.name == unit["component"]), None)
    note = None
    if not comp and since and unit.get("file"):
        chain = follow_renames(unit["file"], since, head)
        if chain:
            final = chain[-1].split(" (")[0]
            comp = next((c for c in head.components if c.file == final), None)
            note = "followed renames: " + " -> ".join(chain)
    if not comp:
        return [], None
    if unit["kind"] == "method":
        if any(m.name == unit["method"] for m in comp.methods):
            return [f"{comp.name}.{unit['method']}"], note
        # Method renamed too: the whole component is the honest answer, at lower confidence (set by caller).
        return ([f"{comp.name}.{m.name}" for m in comp.methods], note) if note else ([], None)
    if unit["kind"] == "component":
        return [f"{comp.name}.{m.name}" for m in comp.methods], note
    # template change: the methods bound to that element
    return [f"{comp.name}.{h}" for h in unit.get("handlers", [])], note


def analyse(number: int, head: CodeMap, graph: Graph) -> BlastRadius:
    pr = fetch_pr(number)
    after = code_map.build(_ensure_commit(pr["merge_commit"]))
    before = code_map.build(pr["base"])

    changed: dict[str, dict] = {}
    method_ids, unmapped = {}, []
    for f in pr["files"]:
        new, old = changed_lines(f["patch"])
        units = units_for(after, f["path"], new) + units_for(before, f["path"], old)
        if not units:
            unmapped.append({"file": f["path"], "why": "no component, method or template element on the changed lines"})
        for u in units:
            u["file"] = f["path"]
            ids, note = resolve(u, head, since=pr["merge_commit"])
            label = f"{u['component']}.{u.get('method') or u['kind']}" if u["kind"] != "template" else \
                f"{u['component']} template line {u['line']} <{u['tag']}>"
            row = changed.setdefault(label, {"file": f["path"], "unit": label, "lines": [], "resolved": ids, "lineage": note})
            row["lines"] = sorted(set(row["lines"]) | set(u.get("lines", [u.get("line")])))
            conf = 1.0 if u["kind"] in ("method", "template") else 0.6
            if note:
                conf *= 0.7   # the code moved since the PR: the mapping is a best guess, say so
            for i in ids:
                method_ids[i] = max(method_ids.get(i, 0), conf)
            if not ids and not any(x.get("unit") == label for x in unmapped):
                unmapped.append({"file": f["path"], "unit": label,
                                 "why": "no longer exists in the code the live app runs (renamed or removed)"})

    impacts: dict[tuple, Impact] = {}

    def add(kind, id_, label, conf, why, observed=True):
        key = (kind, id_)
        cur = impacts.get(key)
        if cur is None or conf > cur.confidence:
            impacts[key] = Impact(kind=kind, id=id_, label=label, confidence=round(conf, 2), why=why, observed=observed)
        elif why[0] not in cur.why:
            cur.why.append(why[0])

    rows = graph.impacted_ui(list(method_ids))
    at_risk_elements, at_risk_templates = set(), set()
    for r in rows:
        base = method_ids[r["method"]] * (r["a_conf"] or 1.0)
        at_risk_templates.add(r["template_element"])
        if r["element"]:
            conf = base * (r["r_conf"] or 0)
            at_risk_elements.add(r["element"])
            add("element", r["element"], f"'{r['label'] or r['t_tag']}' on {r['template']} ({r['auth']})", conf,
                [f"{r['method']}() changed; {r['why']}; matched on screen by {r['r_evidence'] or r['r_method']}"])
            add("screen", r["screen"], f"{r['template']} ({r['auth']})", conf,
                [f"contains '{r['label']}'"])
        elif r["t_text"] or r["t_tag"] in ("button", "a", "input", "textarea"):
            cond = f" (shown only when {r['conditions'][0]})" if r["conditions"] else ""
            add("element", r["template_element"], f"'{r['t_text'] or r['t_tag']}' in code{cond}", base * 0.5,
                [f"{r['method']}() changed; {r['why']}; never seen in the crawl"], observed=False)

    eps = graph.endpoints_of(list(method_ids))
    endpoints = sorted({e["endpoint"] for e in eps})
    for e in eps:
        if not e["observed_by"]:
            add("unmapped_code", e["endpoint"], f"API call {e['endpoint']}", method_ids[e["method"]] * 0.7,
                [f"{e['method']}() can call {e['endpoint']}, but no crawled button triggered it"], observed=False)

    for fl in graph.flows_touching(sorted(at_risk_elements | at_risk_templates)):
        best = max((i.confidence for k, i in impacts.items() if k[1] in fl["via"]), default=0.5)
        add("flow", fl["flow"], fl["name"], best if fl["observed"] else best * 0.8,
            [f"passes through changed UI ({len(fl['via'])} step(s))"], observed=fl["observed"])
    # Flows implied by code whose action calls an endpoint the change reaches
    for fl in graph.run("MATCH (f:Flow) WHERE f.action_call IN $eps "
                        "RETURN f.id AS id, f.name AS name, f.observed AS observed, f.action_call AS call", eps=endpoints):
        add("flow", fl["id"], fl["name"], 0.7 if fl["observed"] else 0.6,
            [f"its final action calls {fl['call']}, which the changed code calls"], observed=fl["observed"])

    coverage_lost = []
    for q in graph.requirements_touching(sorted(at_risk_elements), endpoints):
        if q["at_risk"]:
            conf = max(i.confidence for k, i in impacts.items() if k[1] in q["at_risk"])
            add("requirement", q["id"], q["statement"], conf, [f"proven by changed UI: {len(q['at_risk'])} element(s)"])
        else:
            add("requirement", q["id"], q["statement"], 0.6,
                [f"expects {', '.join(q['eps'])}, which the changed code calls"
                 + (f"; UI evidence {q['gap']}: {q['gap_reason']}" if q["gap"] else "")], observed=bool(q["cov"]))
        if q["only"]:
            coverage_lost.append({"requirement": q["id"], "statement": q["statement"],
                                  "why": "every element that proves it is changed by this PR"})
        elif q["gap"] and q["eps"]:
            coverage_lost.append({"requirement": q["id"], "statement": q["statement"],
                                  "why": f"no UI coverage to re-check it with ({q['gap']}: {q['gap_reason']})"})

    # Page-level requirements (routes, prose behaviours) on pages the change touches, observed or code-only.
    pages = {i.label.split(" (")[0] for i in impacts.values() if i.kind == "screen"}
    pages |= {m.group(1) for i in impacts.values() if i.kind == "flow" for m in [re.search(r" on (/\S*)", i.label)] if m}
    for q in graph.run("MATCH (q:Requirement) WHERE q.kind IN ['route', 'behaviour'] AND any(p IN q.pages WHERE p IN $pages) "
                       "OPTIONAL MATCH (q)-[:HAS_GAP]->(g:Gap) RETURN q.id AS id, q.statement AS statement, q.pages AS pages, "
                       "g.status AS gap", pages=sorted(pages)):
        if ("requirement", q["id"]) not in impacts:
            add("requirement", q["id"], q["statement"], 0.5,
                [f"describes {', '.join(sorted(set(q['pages']) & pages))}, a page this change affects"],
                observed=q["gap"] is None)

    needs_review = [{"item": i.label, "why": "only seen in code, never observed in the running app", "confidence": i.confidence}
                    for i in impacts.values() if not i.observed]
    needs_review += [{"item": i.label, "why": f"low confidence ({i.confidence})", "confidence": i.confidence}
                     for i in impacts.values() if i.observed and i.confidence < SETTINGS.link_review]
    needs_review += [{"item": u.get("unit", u["file"]), "why": u["why"], "confidence": 0.0} for u in unmapped]

    order = {"requirement": 0, "flow": 1, "element": 2, "screen": 3, "unmapped_code": 4}
    return BlastRadius(pr=number, title=pr["title"], url=pr["url"], changed=list(changed.values()),
                       impacts=sorted(impacts.values(), key=lambda i: (order[i.kind], -i.confidence, i.label)),
                       coverage_lost=coverage_lost, needs_review=needs_review)
