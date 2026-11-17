# Blast radius: what does this PR put at risk?

An agent that crawls a live web app, reads its product spec, maps its source code, joins all three
in a Neo4j knowledge graph, and, given a real pull request, tells a QA lead in plain English which
controls, user flows and requirements the change puts at risk, and how sure it is.

- **App:** [RealWorld "Conduit"](https://demo.realworld.show), Angular frontend
- **Code:** [realworld-apps/angular-realworld-example-app](https://github.com/realworld-apps/angular-realworld-example-app)
- **Spec:** the RealWorld [frontend spec](https://docs.realworld.show/specifications/frontend/routing/) and the
  [E2E selectors contract](https://github.com/realworld-apps/realworld/blob/main/specs/e2e/SELECTORS.md)
- **Sample output:** [reports/pr-311.md](reports/pr-311.md) for PR [#311](https://github.com/realworld-apps/angular-realworld-example-app/pull/311)
- **Design document:** [docs/design.md](docs/design.md)
- `data/spec/` and `data/prs/` are cached copies of the public RealWorld spec (MIT) and GitHub PR data, so runs are reproducible offline.

## Run it

Needs Python 3.12+, Docker, git.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env              # add ANTHROPIC_API_KEY (optional, see below)
docker compose up -d              # Neo4j on bolt://localhost:7687, browser UI on http://localhost:7474 (neo4j / blastradius)
git clone https://github.com/realworld-apps/angular-realworld-example-app data/repo

.venv/bin/python -m blast_radius build --recrawl   # crawl (~5 min) + spec + code + links -> Neo4j
.venv/bin/python -m blast_radius pr 311            # -> reports/pr-311.md and reports/pr-311.json
.venv/bin/python -m blast_radius eval 311 --runs 5 # score against evals/gold/pr-311.json
.venv/bin/python -m pytest -q tests
```

`build` without `--recrawl` reuses `artifacts/crawl.json`, so you can rebuild the graph in seconds.

**Without an API key** everything still runs: the spec prose becomes "behaviour" requirements with no UI
evidence (they go to review), and the report uses the deterministic template instead of LLM prose.
The graph, the traversal and the confidence scores don't use the LLM at all.

**Writes to the live app:** the crawler signs up one throwaway account (`br-test-xxxx`, stored in
`.state/account.json`). Every other request that would write (POST/PUT/DELETE) is intercepted and
aborted: buttons are clicked in a "dry run" so we learn which API they call without changing anything.

## What's where

| Stage | File | LLM? |
|---|---|---|
| 1 Crawl (Playwright, logged out + logged in, dry-run clicks) | `blast_radius/crawl.py` | no |
| 2 Spec → requirements (tables parsed; prose bullets by LLM) | `blast_radius/spec.py` | prose only |
| 3 Code map (routes, components, methods, templates, endpoints) | `blast_radius/code_map.py` | no |
| 4 Link layers, absence as Gap nodes, flows | `blast_radius/link.py` | judge for evidence-free requirements only |
| Graph schema + blast-radius queries | `blast_radius/graph.py` | no |
| 5 PR → impacts with confidence | `blast_radius/blast.py` | no |
| 6 Report for a QA lead + verifier | `blast_radius/report.py` | prose only, verified |
| Orchestration (LangGraph) | `blast_radius/pipeline.py` | |
| Evals | `evals/run_eval.py`, `evals/gold/` | |

## Useful Cypher

```cypher
// Requirements we think are missing from the UI, and where we looked
MATCH (q:Requirement)-[:HAS_GAP]->(g:Gap {status:'absent'})-[:SEARCHED]->(s:Screen)
RETURN q.statement, g.reason, collect(s.id);

// From a changed method to the requirements it puts at risk
MATCH (m:Method {id:'EditorComponent.submitForm'})-[:AFFECTS]->(:TemplateElement)-[r:RENDERS]->(e:Element)
      <-[c:COVERED_BY]-(q:Requirement)
RETURN e.label, e.screen, r.confidence, q.statement;
```
