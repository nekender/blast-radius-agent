"""Settings, read from the environment (and a local .env file if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_url: str = os.environ.get("BR_APP_URL", "https://demo.realworld.show")
    repo: str = os.environ.get("BR_REPO", "realworld-apps/angular-realworld-example-app")
    model: str = os.environ.get("BR_MODEL", "claude-sonnet-5-5")
    neo4j_uri: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.environ.get("NEO4J_USER", "neo4j")
    neo4j_password: str = os.environ.get("NEO4J_PASSWORD", "blastradius")

    # Paths
    artifacts: Path = ROOT / "artifacts"
    data: Path = ROOT / "data"
    repo_dir: Path = ROOT / "data" / "repo"
    spec_dir: Path = ROOT / "data" / "spec"
    state_dir: Path = ROOT / ".state"
    cache_dir: Path = ROOT / ".cache"
    reports_dir: Path = ROOT / "reports"

    # Crawl budget (deliberately small: see docs/design.md, "Scope decisions")
    max_pages: int = int(os.environ.get("BR_MAX_PAGES", "40"))
    max_instances_per_template: int = 2

    # Confidence thresholds
    link_accept: float = 0.75     # auto-accept a code<->UI or requirement<->UI link
    link_review: float = 0.45     # between review and accept: kept, flagged for a human


SETTINGS = Settings()
