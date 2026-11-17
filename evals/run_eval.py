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
A run is "correct" when recall == 1, no must_not hits, and its report passes verification.
"""
from __future__ import annotations

import hashlib
import json

from blast_radius import llm, report
from blast_radius.blast import analyse
from blast_radius.config import ROOT, SETTINGS
from blast_radius.graph import Graph
from blast_radius.models import CodeMap


def _hit(item, m: dict) -> bool:
    if item.kind != m["kind"]:
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
    head = CodeMap.model_validate_json((SETTINGS.artifacts / "codemap.json").read_text())
    g = Graph()
    rows, hashes = [], set()
    try:
        for k in range(runs):
            br = analyse(number, head, g)
            h = hashlib.sha1(json.dumps([(i.kind, i.id, i.confidence) for i in br.impacts]).encode()).hexdigest()[:10]
            hashes.add(h)
            s = score(br, gold)
            if llm.available():
                _, meta = report.write(br, use_cache=False)
                s["report_ok"] = meta["writer"] == "llm"
                s["report_errors"] = meta["errors"]
            else:
                s["report_ok"] = None
            s["correct"] = s["recall"] == 1 and not s["must_not_hits"] and s["report_ok"] is not False
            s["hash"] = h
            rows.append(s)
            print(f"run {k + 1}: recall {s['recall']} precision {s['precision']} report_ok {s['report_ok']} hash {h}")
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
    }
    out = ROOT / "evals" / f"results-pr-{number}.json"
    out.write_text(json.dumps({"summary": summary, "runs": rows}, indent=1))
    print(json.dumps(summary, indent=2))
