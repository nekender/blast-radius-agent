"""The UI server: `python -m blast_radius ui` -> http://localhost:8700

- serves the built frontend (web/dist) and the crawl's screenshots (artifacts/screens)
- runs the real pipeline (`pr <n>` or a cached `build`) in a background thread and streams every event to the
  browser over Server-Sent Events: stages, LLM calls with tokens and dollars, agent tool calls, verifier verdicts
- read-only endpoints over the graph and the artifacts for the other views

One run at a time: the pipeline is the real one, and the graph is shared.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import events
from .config import ROOT, SETTINGS

app = FastAPI(title="Blast radius")
WEB = ROOT / "web" / "dist"


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ---------------- live runs ----------------

class Run:
    def __init__(self, kind: str, args: dict):
        self.kind, self.args = kind, args
        self.events: list[dict] = []
        self.done = False
        self.cond = threading.Condition()

    def push(self, e: dict) -> None:
        with self.cond:
            self.events.append(e)
            self.cond.notify_all()


CURRENT: Run | None = None
_run_lock = threading.Lock()


class _Sink:
    """Duck-types queue.Queue.put for events.attach."""

    def __init__(self, run: Run):
        self.run = run

    def put(self, e: dict) -> None:
        self.run.push(e)


def _execute(run: Run) -> None:
    events.attach(_Sink(run))
    t0 = time.monotonic()
    try:
        events.emit("run_start", run=run.kind, **run.args)
        if run.kind == "pr":
            from .pipeline import reason_graph
            out = reason_graph().invoke({"pr": run.args["pr"], "use_cache": not run.args.get("fresh")})
            events.emit("done", ok=True, writer=out.get("writer"), seconds=round(time.monotonic() - t0, 1))
        else:
            from .pipeline import build_graph
            out = build_graph().invoke({"recrawl": False})
            events.emit("done", ok=True, summary=out.get("summary"), seconds=round(time.monotonic() - t0, 1))
    except Exception as e:  # noqa: BLE001 - surface it in the UI
        from .__main__ import _hint
        events.emit("done", ok=False, error=_hint(e) or f"{e.__class__.__name__}: {e}")
    finally:
        events.detach()
        with run.cond:
            run.done = True
            run.cond.notify_all()


class RunRequest(BaseModel):
    pr: int | None = None
    fresh: bool = False
    kind: str = "pr"


@app.post("/api/run")
def start_run(req: RunRequest):
    global CURRENT
    with _run_lock:
        if CURRENT and not CURRENT.done:
            raise HTTPException(409, "a run is already in progress")
        if req.kind == "pr" and not req.pr:
            raise HTTPException(400, "pr number required")
        CURRENT = Run(req.kind, {"pr": req.pr, "fresh": req.fresh} if req.kind == "pr" else {})
        threading.Thread(target=_execute, args=(CURRENT,), daemon=True).start()
    return {"ok": True}


@app.get("/api/stream")
def stream():
    run = CURRENT
    if run is None:
        raise HTTPException(404, "no run")

    def gen():
        i = 0
        while True:
            with run.cond:
                while i >= len(run.events) and not run.done:
                    run.cond.wait(timeout=15)
                    if i >= len(run.events) and not run.done:
                        break
                batch, finished = run.events[i:], run.done
            for e in batch:
                yield f"data: {json.dumps(e, default=str)}\n\n"
            i += len(batch)
            if finished and i >= len(run.events):
                return
            if not batch:
                yield ": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- read-only views ----------------

@app.get("/api/overview")
def overview():
    links = _read(SETTINGS.artifacts / "links.json", {}) or {}
    reports = sorted(int(p.stem.split("-")[1]) for p in SETTINGS.reports_dir.glob("pr-*.json"))
    return {"summary": links.get("summary", {}), "build_cost": _read(SETTINGS.artifacts / "build_cost.json"),
            "explore_cost": _read(SETTINGS.artifacts / "explore_cost.json"), "reports": reports,
            "model": SETTINGS.model, "app_url": SETTINGS.app_url, "repo": SETTINGS.repo}


@app.get("/api/inputs")
def inputs():
    """Everything the system takes in, with links to the real thing: app, code, spec, tests, PRs, this repo."""
    from collections import Counter
    from .code_map import git
    cm = _read(SETTINGS.artifacts / "codemap.json", {}) or {}
    reqs = _read(SETTINGS.artifacts / "requirements.json", []) or []
    crawl = _read(SETTINGS.artifacts / "crawl.json", {}) or {}
    links = _read(SETTINGS.artifacts / "links.json", {}) or {}
    kinds = Counter(c["kind"] for c in cm.get("components", []))
    try:
        head = git("log", "-1", "--format=%h · %ad", "--date=short").strip()
    except Exception:  # noqa: BLE001
        head = cm.get("commit", "")[:7]
    by_source = Counter(r["source"].split("#")[0] for r in reqs)
    e2e = SETTINGS.spec_dir / "e2e"
    spec_files = sorted(p.name for p in e2e.glob("*.spec.ts"))
    prs = []
    for n in sorted((int(p.stem) for p in (SETTINGS.data / "prs").glob("*.json")), reverse=True):
        d = _read(SETTINGS.data / "prs" / f"{n}.json", {})
        if (SETTINGS.reports_dir / f"pr-{n}.json").exists():
            prs.append({"number": n, "title": d.get("title"), "url": d.get("url"), "files": [f["path"] for f in d.get("files", [])],
                        "merge_commit": (d.get("merge_commit") or "")[:7]})
    home = next((s for s in crawl.get("screens", []) if s["id"] == "/|anon"), None)
    gh = "https://github.com/realworld-apps"
    return {
        "app": {"url": SETTINGS.app_url, "screens": len(crawl.get("screens", [])), "elements": len(crawl.get("elements", [])),
                "screenshot": home.get("screenshot") if home else None},
        "code": {"repo": SETTINGS.repo, "url": f"https://github.com/{SETTINGS.repo}", "head": head,
                 "routes": len(cm.get("routes", [])), "components": kinds.get("component", 0), "services": kinds.get("service", 0),
                 "directives": kinds.get("directive", 0), "methods": sum(len(c["methods"]) for c in cm.get("components", [])),
                 "template_elements": len(cm.get("template_elements", [])),
                 "endpoints": len({e for c in cm.get("components", []) for e in c.get("endpoints", {}).values()}),
                 "route_list": list(dict.fromkeys(r["template"] for r in cm.get("routes", [])))},
        "spec": {"docs": "https://docs.realworld.show/specifications/frontend/routing/",
                 "files": [{"name": "routing.md", "url": f"{gh}/realworld/blob/main/docs/src/content/docs/specifications/frontend/routing.md", "read_by": "read by an LLM (prose)", "requirements": by_source.get("docs/specifications/frontend/routing.md", 0)},
                           {"name": "SELECTORS.md", "url": f"{gh}/realworld/blob/main/specs/e2e/SELECTORS.md", "read_by": "read by code (tables)", "requirements": by_source.get("specs/e2e/SELECTORS.md", 0)},
                           {"name": "templates.md", "url": f"{gh}/realworld/blob/main/docs/src/content/docs/specifications/frontend/templates.md", "read_by": "context for the LLM", "requirements": 0}],
                 "requirements": len(reqs)},
        "tests": {"url": f"{gh}/realworld/tree/main/specs/e2e", "files": spec_files,
                  "tests": links.get("summary", {}).get("tests"), "steps": links.get("summary", {}).get("test_steps"),
                  "linked": links.get("summary", {}).get("tests_linked")},
        "prs": prs,
        "project": {"url": "https://github.com/nekender/blast-radius-agent",
                    "design": "https://github.com/nekender/blast-radius-agent/blob/main/docs/design.md"},
    }


@app.get("/api/report/{pr}")
def report(pr: int):
    data = _read(SETTINGS.reports_dir / f"pr-{pr}.json")
    if data is None:
        raise HTTPException(404, "no report yet: run it first")
    md = (SETTINGS.reports_dir / f"pr-{pr}.md")
    return {"blast": data, "markdown": md.read_text() if md.exists() else ""}


@app.get("/api/graph/{pr}")
def graph(pr: int):
    """The PR's blast radius as a layered graph: changed code -> markup -> live controls -> screens -> journeys,
    tests and requirements, with the edges that connect them."""
    from .graph import Graph
    g = Graph()
    try:
        risk = g.run("""MATCH (p:PullRequest {number: $n})-[r:PUTS_AT_RISK]->(x)
                        RETURN x.id AS id, labels(x)[0] AS kind, coalesce(x.name, x.label, x.statement, x.text, x.id) AS label,
                               r.confidence AS confidence, r.observed AS observed, r.why AS why""", n=pr)
        methods = g.run("""MATCH (p:PullRequest {number: $n})-[c:CHANGES]->(m:Method)
                           RETURN m.id AS id, m.file AS file, m.start AS start, m.end AS end, c.lineage AS lineage""", n=pr)
        ids = [r["id"] for r in risk]
        mids = [m["id"] for m in methods]
        code_ui = g.run("""MATCH (m:Method)-[a:AFFECTS]->(t:TemplateElement)-[r:RENDERS]->(e:Element)
                           WHERE m.id IN $mids AND e.id IN $ids
                           RETURN m.id AS m, t.id AS t, t.tag AS tag, t.text AS text, t.line AS line, t.file AS file,
                                  e.id AS e, a.why AS why, r.confidence AS conf, r.evidence AS evidence""",
                        mids=mids, ids=ids)
        rel = g.run("""MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids AND type(r) IN
                       ['HAS', 'STEP', 'USES', 'COVERED_BY', 'TESTED_BY', 'NAVIGATES_TO']
                       RETURN DISTINCT a.id AS src, b.id AS dst, type(r) AS type""", ids=ids)
    finally:
        g.close()
    if not risk and not methods:
        raise HTTPException(404, "no analysis for this PR in the graph: run it first")
    nodes = {m["id"]: {"id": m["id"], "kind": "Method", "label": m["id"], "confidence": 1.0, "observed": True,
                       "detail": {"file": m["file"], "lines": f"{m['start']}-{m['end']}", "lineage": m["lineage"]}}
             for m in methods}
    for r in risk:
        nodes[r["id"]] = {"id": r["id"], "kind": r["kind"], "label": r["label"], "confidence": r["confidence"],
                          "observed": r["observed"], "detail": {"why": r["why"]}}
    edges = []
    for c in code_ui:
        tid = c["t"]
        nodes.setdefault(tid, {"id": tid, "kind": "TemplateElement", "label": f"<{c['tag']}> {c['text'] or ''}".strip(),
                               "confidence": 1.0, "observed": True,
                               "detail": {"file": c["file"], "line": c["line"]}})
        edges.append({"src": c["m"], "dst": tid, "type": "AFFECTS", "label": c["why"]})
        edges.append({"src": tid, "dst": c["e"], "type": "RENDERS", "label": c["evidence"], "confidence": c["conf"]})
    edges += [{"src": r["src"], "dst": r["dst"], "type": r["type"]} for r in rel]
    seen, uniq = set(), []
    for e in edges:
        k = (e["src"], e["dst"], e["type"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return {"nodes": list(nodes.values()), "edges": uniq}


@app.get("/api/coverage")
def coverage():
    reqs = _read(SETTINGS.artifacts / "requirements.json", []) or []
    links = _read(SETTINGS.artifacts / "links.json", {}) or {}
    gaps = {g["requirement_id"]: g for g in links.get("gaps", [])}
    shown = {}
    for l in links.get("requirement_links", []):
        shown[l["src"]] = max(shown.get(l["src"], 0), l["confidence"])
    tests = {}
    for l in links.get("tested_by", []):
        tests.setdefault(l["src"], set()).add(l["dst"])
    review = {r.get("requirement"): r["why"] for r in links.get("review", []) if r.get("requirement")}
    out = []
    for r in reqs:
        g = gaps.get(r["id"])
        ui = g["status"] if g else ("covered" if shown.get(r["id"], 0) >= SETTINGS.link_review
                                    else "weak" if r["id"] in shown else "unknown")
        out.append({"id": r["id"], "statement": r["statement"], "source": r["source"], "needs": r["needs"],
                    "kind": r["kind"], "pages": r["pages"], "ui": ui, "ui_confidence": round(shown.get(r["id"], 0), 2),
                    "gap_reason": g["reason"] if g else None, "partial": review.get(r["id"]),
                    "tests": len(tests.get(r["id"], ())), "test_ids": sorted(tests.get(r["id"], ()))[:8]})
    return {"requirements": out, "summary": links.get("summary", {})}


@app.get("/api/explore")
def explore():
    trace = _read(SETTINGS.artifacts / "explore_trace.json", {}) or {}
    crawl = _read(SETTINGS.artifacts / "crawl.json", {}) or {}
    shots = {s["id"]: s.get("screenshot") for s in crawl.get("screens", [])}
    return {"goals": trace.get("goals", []), "trace": trace.get("trace", []), "finish": trace.get("finish"),
            "created": trace.get("created", []), "deleted": trace.get("deleted", []),
            "blocked_writes": trace.get("blocked_writes", []), "recorded": trace.get("recorded_owner_screens", []),
            "screenshots": {sid: shots.get(sid) for sid in trace.get("recorded_owner_screens", [])},
            "cost": _read(SETTINGS.artifacts / "explore_cost.json")}


@app.get("/api/screens")
def screens():
    crawl = _read(SETTINGS.artifacts / "crawl.json", {}) or {}
    return [{"id": s["id"], "template": s["template"], "auth": s["auth"], "screenshot": s.get("screenshot"),
             "states": len(s.get("a11y_states", [])), "url": s["sample_urls"][0] if s.get("sample_urls") else None}
            for s in crawl.get("screens", [])]


@app.get("/api/evals")
def evals():
    out = {}
    for p in sorted((ROOT / "evals").glob("results-pr-*.json")):
        out[p.stem.split("-")[-1]] = _read(p)
    gold = {p.stem.split("-")[-1]: _read(p) for p in sorted((ROOT / "evals" / "gold").glob("pr-*.json"))}
    return {"results": out, "gold": gold}


# ---------------- static ----------------

app.mount("/artifacts", StaticFiles(directory=str(SETTINGS.artifacts)), name="artifacts")
if (WEB / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(WEB / "assets")), name="assets")


@app.get("/{path:path}")
def spa(path: str):
    index = WEB / "index.html"
    if not index.exists():
        return {"error": "frontend not built: cd web && npm install && npm run build"}
    # index.html must never be cached: it names the current hashed bundle (the bundles themselves can be)
    return FileResponse(index, headers={"Cache-Control": "no-store"})


def serve(port: int = 8700) -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
