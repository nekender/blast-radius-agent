"""Eval: is the blast radius right, and is it the same every time?

Two questions, answered separately:
1. Correctness against a hand-labelled gold set (evals/gold/pr-<n>.json):
   - recall    = share of `must` items the run found
   - precision = share of reported items that are `must` or `acceptable`
   - any `must_not` item reported is a hard failure
2. Stability over N runs on the same input:
   - the deterministic part (graph traversal) must produce the identical impact set every run (hash)
   - the LLM part (the report) is run with the cache off; each run must pass verification
     (no invented ids, every high-confidence item mentioned). pass^k = all k runs pass.
3b. The assessment agent (fresh each run): it must finish, the first flow it ranks must match the gold
   `top_flow_calls` where given, and we count adjustments it tried without inspecting the item first
   (rejected by the guard, reported as a discipline metric).
A run is "correct" when recall == 1, no must_not hits, its report passes verification, and the agent's top
flow is right.
"""
from __future__ import annotations

import hashlib
import json

from blast_radius import llm, report
from blast_radius.assess import assess
from blast_radius.blast import analyse, fetch_pr
from blast_radius.config import ROOT, SETTINGS
from blast_radius.graph import Graph
from blast_radius.models import CodeMap


FLOW_CALLS: dict[str, list[str]] = {}


def _hit(item, m: dict) -> bool:
    if item.kind != m["kind"]:
        return False
    # Flows are identified by the API call their final action makes: their names are written by the LLM.
    if "calls_contains" in m and not any(m["calls_contains"] == c for c in FLOW_CALLS.get(item.id, [])):
        return False
    if "label_contains" in m and m["label_contains"].lower() not in item.label.lower():
        return False
    if "where" in m and m["where"] not in item.label:
        return False
    return True


def score(br, gold: dict) -> dict:
    items = [i for i in br.impacts]
    must_found = [m for m in gold["must"] if any(_hit(i, m) for i in items)]
    ok = [i for i in items if any(_hit(i, m) for m in gold["must"] + gold["acceptable"])]
    bad = [i.label for i in items if any(_hit(i, m) for m in gold["must_not"])]
    return {"recall": round(len(must_found) / len(gold["must"]), 2),
            "precision": round(len(ok) / max(len(items), 1), 2),
            "missed": [m for m in gold["must"] if m not in must_found],
            "unexpected": [i.label for i in items if i not in ok][:10],
            "must_not_hits": bad}


def run(number: int, runs: int) -> None:
    gold = json.loads((ROOT / "evals" / "gold" / f"pr-{number}.json").read_text())
    links = json.loads((SETTINGS.artifacts / "links.json").read_text())
    FLOW_CALLS.update({f["id"]: f["calls"] for f in links["flows"]})
    head = CodeMap.model_validate_json((SETTINGS.artifacts / "codemap.json").read_text())
    g = Graph()
    rows, hashes = [], set()
    try:
        for k in range(runs):
            br = analyse(number, head, g)
            h = hashlib.sha1(json.dumps([(i.kind, i.id, i.confidence) for i in br.impacts]).encode()).hexdigest()[:10]
            hashes.add(h)
            if llm.available():
                br = assess(br, fetch_pr(number), head, g, use_cache=False)
                a = br.assessment or {}
                flows = [i for i in br.impacts if i.kind == "flow"]
                top = FLOW_CALLS.get(flows[0].id, []) if flows else []
                s_agent = {"agent_finished": a.get("finished", False),
                           "agent_ungrounded": sum("inspecting" in r["rejected_because"] for r in a.get("rejected", [])),
                           "agent_top_flow_ok": gold.get("top_flow_calls") in top if gold.get("top_flow_calls") else None,
                           "agent_tool_calls": a.get("tool_calls", 0)}
                tests = [i for i in br.impacts if i.kind == "test"]
                if gold.get("top_test_contains"):
                    s_agent["agent_top_test_ok"] = bool(tests) and gold["top_test_contains"] in tests[0].label.lower()
            else:
                s_agent = {}
            s = score(br, gold) | s_agent
            if llm.available():
                _, meta = report.write(br, use_cache=False)
                s["report_ok"] = meta["writer"] == "llm"
                s["report_errors"] = meta["errors"]
            else:
                s["report_ok"] = None
            # Ungrounded adjustments are rejected by the guard, so they can't make the output wrong; they're
            # tracked separately as agent discipline, not as run failures.
            s["correct"] = (s["recall"] == 1 and not s["must_not_hits"] and s["report_ok"] is not False
                            and s.get("agent_top_flow_ok") is not False and s.get("agent_top_test_ok") is not False)
            s["hash"] = h
            rows.append(s)
            print(f"run {k + 1}: recall {s['recall']} precision {s['precision']} report_ok {s['report_ok']} "
                  f"agent_top_flow_ok {s.get('agent_top_flow_ok')} top_test_ok {s.get('agent_top_test_ok')} tool_calls {s.get('agent_tool_calls')} hash {h}")
    finally:
        g.close()
    summary = {
        "pr": number, "runs": runs,
        "deterministic_stage_identical": len(hashes) == 1,
        "correct_runs": sum(r["correct"] for r in rows),
        "pass^k": all(r["correct"] for r in rows),
        "recall": rows[0]["recall"], "precision": rows[0]["precision"],
        "missed": rows[0]["missed"], "unexpected": rows[0]["unexpected"], "must_not_hits": rows[0]["must_not_hits"],
        "report_pass_rate": None if rows[0]["report_ok"] is None else sum(r["report_ok"] for r in rows) / runs,
        "agent_top_flow_ok_rate": None if rows[0].get("agent_top_flow_ok") is None
        else sum(bool(r["agent_top_flow_ok"]) for r in rows) / runs,
        "agent_ungrounded_adjustments": sum(r.get("agent_ungrounded", 0) for r in rows),
        "agent_top_test_ok_rate": None if rows[0].get("agent_top_test_ok") is None
        else sum(bool(r["agent_top_test_ok"]) for r in rows) / runs,
    }
    out = ROOT / "evals" / f"results-pr-{number}.json"
    out.write_text(json.dumps({"summary": summary, "runs": rows}, indent=1))
    print(json.dumps(summary, indent=2))
