"""CLI.

  python -m blast_radius build [--recrawl]     crawl + spec + code + links -> Neo4j
  python -m blast_radius explore               LLM agent reaches owner-only pages (creates + deletes 1 test article)
  python -m blast_radius pr 311                blast-radius report -> reports/pr-311.md
  python -m blast_radius eval 311 --runs 10    run the PR analysis N times and score against evals/gold
"""
from __future__ import annotations

import argparse
import json

from .config import SETTINGS


def main() -> None:
    ap = argparse.ArgumentParser(prog="blast_radius")
    sub = ap.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("explore", help="LLM agent reaches owner-only states, then the graph is rebuilt")
    x.add_argument("--turns", type=int, default=25)
    b = sub.add_parser("build")
    b.add_argument("--recrawl", action="store_true")
    p = sub.add_parser("pr")
    p.add_argument("number", type=int)
    p.add_argument("--no-cache", action="store_true", help="call the LLM fresh instead of using cached answers")
    e = sub.add_parser("eval")
    e.add_argument("number", type=int)
    e.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()

    if args.cmd == "build":
        from .pipeline import build_graph
        out = build_graph().invoke({"recrawl": args.recrawl})
        print(json.dumps(out["summary"], indent=2))
    elif args.cmd == "explore":
        from .explore import explore
        from .pipeline import build_graph
        rep = explore(args.turns)
        print(json.dumps({k: rep.get(k) for k in ("recorded_owner_screens", "created", "deleted", "blocked_writes", "finish")},
                         indent=2, default=str))
        print(json.dumps(build_graph().invoke({"recrawl": False})["summary"], indent=2))
    elif args.cmd == "pr":
        from .pipeline import reason_graph
        out = reason_graph().invoke({"pr": args.number, "use_cache": not args.no_cache})
        print(f"report: {SETTINGS.reports_dir / f'pr-{args.number}.md'}  (writer: {out['writer']})")
    elif args.cmd == "eval":
        from evals.run_eval import run
        run(args.number, args.runs)


if __name__ == "__main__":
    main()
