# Blast radius: what does this PR put at risk?

An agent that crawls a live web app, reads its product spec, maps its source code, joins all three
in a Neo4j knowledge graph, and, given a real pull request, tells a QA lead in plain English which
controls, user flows and requirements the change puts at risk, and how sure it is.

The graph is the grounding; two LLM agents work on top of it, with guardrails in code:
- an **exploration agent** that drives the browser to reach states the crawl can't (e.g. pages only an article's
  author sees), under a write policy enforced at the network layer, with verified cleanup;
- an **assessment agent** that reads the diff and the graph through tools and re-ranks the blast radius by what the
  change actually does, only for items it inspected, within bounds, never removing anything.

- **App:** [RealWorld "Conduit"](https://demo.realworld.show), Angular frontend
- **Code:** [realworld-apps/angular-realworld-example-app](https://github.com/realworld-apps/angular-realworld-example-app)
- **Tests:** RealWorld's [Playwright E2E suite](https://github.com/realworld-apps/realworld/tree/main/specs/e2e) (129 tests), parsed into the graph
- **Spec:** the RealWorld [frontend spec](https://docs.realworld.show/specifications/frontend/routing/) and the
  [E2E selectors contract](https://github.com/realworld-apps/realworld/blob/main/specs/e2e/SELECTORS.md)
- **Sample output:** [reports/pr-311.md](reports/pr-311.md) for PR [#311](https://github.com/realworld-apps/angular-realworld-example-app/pull/311)
- **Design document:** [docs/design.md](docs/design.md) (PDF: [docs/design.pdf](docs/design.pdf))
- `data/spec/` (including `data/spec/e2e/`) and `data/prs/` are cached copies of the public RealWorld spec (MIT) and GitHub PR data, so runs are reproducible offline.

## Run it

Needs Python 3.12+ (tested on 3.14), Docker, git. Ports 7474 and 7687 must be free for Neo4j.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env              # add ANTHROPIC_API_KEY (optional, see below)
docker compose up -d              # Neo4j on bolt://localhost:7687, browser UI on http://localhost:7474 (neo4j / blastradius)
git clone https://github.com/realworld-apps/angular-realworld-example-app data/repo

.venv/bin/python -m blast_radius build --recrawl   # crawl (~5 min) + spec + code + links -> Neo4j
.venv/bin/python -m blast_radius explore           # exploration agent closes owner-only gaps, then rebuilds (needs a key)
.venv/bin/python -m blast_radius pr 311            # -> reports/pr-311.md and reports/pr-311.json
.venv/bin/python -m blast_radius eval 311 --runs 5 # score against evals/gold/pr-311.json
.venv/bin/python -m pytest -q tests
```

`build` without `--recrawl` reuses `artifacts/crawl.json`, so you can rebuild the graph in seconds.

`pr <n>` also writes the result into Neo4j as a `PullRequest` node (see the Cypher below).
`artifacts/screens/` holds a screenshot of every screen the crawler saw; `artifacts/dom/` the matching DOM snapshot;
`artifacts/a11y/` the normalised accessibility snapshot used for state identity.

**Cost:** every report ends with what it cost (tokens, dollars, seconds by stage). Typical PR: about $0.10 and
35 s on Claude Sonnet 5.5; a from-scratch graph build about $0.07; an exploration run about $0.07. Details in
`artifacts/build_cost.json`, `artifacts/explore_cost.json`, `evals/results-pr-*.json` and the design doc.

**Without an API key** everything still runs (tested from a fresh clone: 79 of 106 requirements covered instead of 92): the spec prose becomes "behaviour" requirements with no UI
evidence (they go to review), and the report uses the deterministic template instead of LLM prose.
The graph, the traversal and the confidence scores don't use the LLM at all.

**Writes to the live app:** the crawler signs up one throwaway account (`br-test-xxxx`, stored in
`.state/account.json`). Every other request that would write (POST/PUT/DELETE) is intercepted and
aborted: buttons are clicked in a "dry run" so we learn which API they call without changing anything.
The one exception is `explore`: it may create **one** article titled `br-test: ...` and delete what it created;
every other write is still aborted, cleanup runs in a `finally` and is checked (404), and leftovers from a crashed
run are deleted at the start of the next. The committed `artifacts/` already include an exploration run
(`artifacts/explore_trace.json`), so you don't need to run it to see the results.

## What's where

| Stage | File | LLM? |
|---|---|---|
| 1 Crawl (Playwright, logged out + logged in, dry-run clicks) | `blast_radius/crawl.py` | no |
| 1b Exploration agent (browser tools, write policy, cleanup) | `blast_radius/explore.py` | **agent** |
| 2 Spec → requirements (tables parsed; prose bullets by LLM) | `blast_radius/spec.py` | prose only |
| 2b Test suite → Test nodes, USES / TESTED_BY | `blast_radius/suite.py` | no |
| 3 Code map (routes, components, methods, templates, endpoints) | `blast_radius/code_map.py` | no |
| 4 Link layers, absence as Gap nodes, flows | `blast_radius/link.py` | judge for evidence-free requirements only |
| Graph schema + blast-radius queries | `blast_radius/graph.py` | no |
| 5 PR → impacts with confidence | `blast_radius/blast.py` | no |
| 5b Assessment agent (graph tools, grounding + bounds) | `blast_radius/assess.py` | **agent** |
| Tool-using agent loop (shared, traced) | `blast_radius/llm.py` | |
| 6 Report for a QA lead + verifier | `blast_radius/report.py` | prose only, verified |
| Orchestration (LangGraph) | `blast_radius/pipeline.py` | |
| Evals | `evals/run_eval.py`, `evals/gold/` | |

## Useful Cypher

```cypher
// Requirements we think are missing from the UI, and where we looked
MATCH (q:Requirement)-[:HAS_GAP]->(g:Gap {status:'absent'})-[:SEARCHED]->(s:Screen)
RETURN q.statement, g.reason, collect(s.id);

// What a PR puts at risk, straight from the graph (after `pr 311`)
MATCH (p:PullRequest {number: 311})-[r:PUTS_AT_RISK]->(x)
RETURN labels(x)[0] AS kind, coalesce(x.name, x.label, x.statement, x.id) AS item, r.confidence, r.observed
ORDER BY r.confidence DESC;

// Tests to re-run for a PR, most important first
MATCH (p:PullRequest {number: 311})-[r:PUTS_AT_RISK]->(t:Test)
RETURN t.label, r.confidence ORDER BY r.confidence DESC;

// Requirements no test checks
MATCH (q:Requirement) WHERE NOT (q)-[:TESTED_BY]->(:Test) RETURN q.statement;

// From a changed method to the requirements it puts at risk
MATCH (m:Method {id:'EditorComponent.submitForm'})-[:AFFECTS]->(:TemplateElement)-[r:RENDERS]->(e:Element)
      <-[c:COVERED_BY]-(q:Requirement)
RETURN e.label, e.screen, r.confidence, q.statement;
```
