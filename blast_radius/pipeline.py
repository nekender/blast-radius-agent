"""The agent, as two LangGraph graphs.

build:   crawl ─┬─> ingest_spec ─┬─> link + test suite ─> load_graph
                └─> map_code ────┘
         (crawl is skipped when artifacts/crawl.json exists and --recrawl isn't set)

reason:  analyse_pr ─> assess (agent) ─> write_report ─> verify ──ok──> save (report files + PullRequest node in Neo4j)
                            ^            │ errors, first time
                            └────────────┘
                                         │ errors again ─> template_report ─> save

Why LangGraph here: named stages with typed state, the spec and code stages run in parallel, and
the report loop (write -> verify -> rewrite once -> fall back) is an explicit conditional edge, not
a retry buried in a function. Every stage except the spec prose and the report prose is plain code.
"""
from __future__ import annotations

import json
import operator
from typing import Annotated, Any, TypedDict

import time

from langgraph.graph import END, START, StateGraph

from . import code_map, crawl as crawl_mod, link, llm, report, spec
from .config import SETTINGS
from .graph import Graph
from .models import BlastRadius, CodeMap, Crawl


def timed(name: str, fn):
    """Run a node inside an LLM cost stage and record its wall-clock seconds in state['timings']."""
    def run(s):
        t0 = time.monotonic()
        with llm.stage(name):
            out = fn(s) or {}
        # merged by the state's reducer; nodes that share a stage name (write, verify, render) add up
        return {**out, "timings": {name: round(s.get("timings", {}).get(name, 0) + time.monotonic() - t0, 1)}}
    return run


class BuildState(TypedDict, total=False):
    timings: Annotated[dict, operator.or_]
    recrawl: bool
    crawl: Crawl
    reqs: list
    codemap: CodeMap
    summary: dict


def n_crawl(s: BuildState) -> BuildState:
    llm.reset_ledger()
    path = SETTINGS.artifacts / "crawl.json"
    if path.exists() and not s.get("recrawl"):
        return {"crawl": Crawl.model_validate_json(path.read_text())}
    return {"crawl": crawl_mod.crawl(path)}


def n_spec(s: BuildState) -> BuildState:
    reqs = spec.ingest()
    (SETTINGS.artifacts / "requirements.json").write_text(json.dumps([r.model_dump() for r in reqs], indent=1))
    return {"reqs": reqs}


def n_code(s: BuildState) -> BuildState:
    cm = code_map.build("HEAD")
    code_map.save(cm)
    return {"codemap": cm}


def n_link_and_load(s: BuildState) -> BuildState:
    t_link = time.monotonic()
    cr, cm, reqs = s["crawl"], s["codemap"], s["reqs"]
    idx = link.Index(cm)
    code_ui, review_ui = link.link_code_ui(cr, cm, idx)
    methods = link.link_methods(cm, idx)
    req_links, gaps, review_req = link.link_requirements(reqs, cr, cm, idx)
    flows = link.discover_flows(cr, cm, idx)
    from . import suite
    tests = suite.parse_suite()
    uses, tested, test_stats = suite.link_tests(tests, cr, reqs, req_links)
    g = Graph()
    g.load(cr, cm, reqs, code_ui, methods, req_links, gaps, flows)
    g.load_tests(tests, uses, tested)
    # A rebuild recreates every node, so re-attach the PRs analysed so far (from reports/*.json): the PR history
    # in the graph survives a rebuild. An item that no longer exists simply gets no edge.
    for f in sorted(SETTINGS.reports_dir.glob("pr-*.json")):
        try:
            g.save_blast(BlastRadius.model_validate_json(f.read_text()))
        except Exception:  # noqa: BLE001 - a report from an older schema: skip it
            pass
    g.close()
    covered = {l.src for l in req_links} - {g.requirement_id for g in gaps}
    summary = {
        "screens": len(cr.screens), "elements": len(cr.elements), "requirements": len(reqs),
        "covered": len(covered), "absent": sum(g.status == "absent" for g in gaps),
        "unknown": sum(g.status == "unknown" for g in gaps), "partial": sum(g.status == "partial" for g in gaps),
        "code_ui_links": sum(l.kind == "RENDERS" for l in code_ui),
        "elements_linked_to_code": len({l.dst for l in code_ui if l.kind == "RENDERS"}),
        "flows_observed": sum(f.observed for f in flows), "flows_from_code_only": sum(not f.observed for f in flows),
        "needs_review": len(review_ui) + len(review_req),
        **test_stats,
    }
    cost = llm.summarize()
    summary["llm_usd"] = cost["total"]["usd"]
    summary["llm_usd_from_scratch"] = cost["total"]["usd_uncached"]
    (SETTINGS.artifacts / "build_cost.json").write_text(json.dumps(
        {"llm": cost, "seconds_by_stage": {**s.get("timings", {}), "link": round(time.monotonic() - t_link, 1)}}, indent=1))
    (SETTINGS.artifacts / "links.json").write_text(json.dumps({
        "summary": summary, "gaps": [g.model_dump() for g in gaps], "flows": [f.model_dump() for f in flows],
        "review": review_ui + review_req, "code_ui": [l.model_dump() for l in code_ui],
        "requirement_links": [l.model_dump() for l in req_links],
        "test_uses": [l.model_dump() for l in uses], "tested_by": [l.model_dump() for l in tested]}, indent=1))
    return {"summary": summary}


def build_graph():
    g = StateGraph(BuildState)
    g.add_node("crawl", timed("crawl", n_crawl))
    g.add_node("ingest_spec", timed("spec", n_spec))
    g.add_node("map_code", timed("code", n_code))
    g.add_node("link_and_load", timed("link", n_link_and_load))
    g.add_edge(START, "crawl")
    g.add_edge("crawl", "ingest_spec")
    g.add_edge("crawl", "map_code")
    g.add_edge(["ingest_spec", "map_code"], "link_and_load")
    g.add_edge("link_and_load", END)
    return g.compile()


# ---------------- reason ----------------

class ReasonState(TypedDict, total=False):
    timings: Annotated[dict, operator.or_]
    pr: int
    use_cache: bool
    blast: BlastRadius
    written: Any
    errors: list[str]
    attempts: int
    markdown: str
    writer: str


def n_analyse(s: ReasonState) -> ReasonState:
    from .blast import analyse
    llm.reset_ledger()
    head = CodeMap.model_validate_json((SETTINGS.artifacts / "codemap.json").read_text())
    g = Graph()
    try:
        br = analyse(s["pr"], head, g)
    finally:
        g.close()
    return {"blast": br, "attempts": 0, "errors": []}


def n_assess(s: ReasonState) -> ReasonState:
    from .assess import assess
    from .blast import fetch_pr
    head = CodeMap.model_validate_json((SETTINGS.artifacts / "codemap.json").read_text())
    g = Graph()
    try:
        br = assess(s["blast"], fetch_pr(s["pr"]), head, g, use_cache=s.get("use_cache", True))
    finally:
        g.close()
    return {"blast": br}


def n_write(s: ReasonState) -> ReasonState:
    from . import llm
    if not llm.available():
        return {"written": None, "errors": ["no ANTHROPIC_API_KEY"], "attempts": 2}
    facts = json.dumps(report._facts(s["blast"]), indent=1)
    if s.get("errors"):
        facts += f"\n\nFix these problems from your last draft: {s['errors']}"
    try:
        w = llm.structured(report.SYSTEM, facts, report.Written, use_cache=s.get("use_cache", True), max_tokens=8000)
    except Exception as e:  # noqa: BLE001 - API error or two invalid answers: fall through to the template report
        return {"written": None, "errors": [f"report writer failed: {e.__class__.__name__}"], "attempts": 2}
    return {"written": w, "attempts": s.get("attempts", 0) + 1}


def n_verify(s: ReasonState) -> ReasonState:
    if s.get("written") is None:
        return {}
    return {"errors": report.verify(s["written"], s["blast"])}


def route_after_verify(s: ReasonState) -> str:
    if s.get("written") is not None and not s.get("errors"):
        return "render"
    return "write_report" if s.get("attempts", 0) < 2 else "template_report"


def _account(s: ReasonState) -> None:
    """All model calls for this PR are done by now: attach tokens, dollars and seconds to the result."""
    s["blast"].cost = {"llm": llm.summarize(), "seconds_by_stage": s.get("timings", {}),
                       "seconds_total": round(sum(s.get("timings", {}).values()), 1)}


def n_render(s: ReasonState) -> ReasonState:
    _account(s)
    return {"markdown": report.render(s["written"], s["blast"]), "writer": "llm"}


def n_template(s: ReasonState) -> ReasonState:
    _account(s)
    return {"markdown": report.template_report(s["blast"]), "writer": "template"}


def n_save(s: ReasonState) -> ReasonState:
    out = SETTINGS.reports_dir
    out.mkdir(exist_ok=True)
    (out / f"pr-{s['pr']}.md").write_text(s["markdown"])
    (out / f"pr-{s['pr']}.json").write_text(s["blast"].model_dump_json(indent=1))
    g = Graph()
    try:
        g.save_blast(s["blast"])
    finally:
        g.close()
    return {}


def reason_graph():
    g = StateGraph(ReasonState)
    stages = {"analyse_pr": "traverse", "assess": "assess", "write_report": "report", "verify": "report",
              "render": "report", "template_report": "report", "save": "save"}
    for name, fn in [("analyse_pr", n_analyse), ("assess", n_assess), ("write_report", n_write), ("verify", n_verify),
                     ("render", n_render), ("template_report", n_template), ("save", n_save)]:
        g.add_node(name, timed(stages[name], fn))
    g.add_edge(START, "analyse_pr")
    g.add_edge("analyse_pr", "assess")
    g.add_edge("assess", "write_report")
    g.add_edge("write_report", "verify")
    g.add_conditional_edges("verify", route_after_verify, ["render", "write_report", "template_report"])
    g.add_edge("render", "save")
    g.add_edge("template_report", "save")
    g.add_edge("save", END)
    return g.compile()
