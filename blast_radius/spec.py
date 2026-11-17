"""Stage 2 · Ingest the spec.

Two sources, two methods, on purpose:
- specs/e2e/SELECTORS.md is already structured (tables of names, texts, classes, routes, endpoints).
  Parsed deterministically: no reason to pay an LLM to read a table.
- docs/.../frontend/routing.md (+ templates.md as context) is prose. The LLM turns each bullet into a
  Requirement with the UI evidence that would prove it, the pages, and the viewer state it needs.
  Without an API key, prose bullets become 'behaviour' requirements with no evidence (they go to review).
"""
from __future__ import annotations

import hashlib
import re

from . import llm
from .config import SETTINGS
from .models import Evidence, Requirement, SpecRequirements

PAGE_TEMPLATES = {
    "home": ["/"], "home / global feed": ["/"], "login": ["/login"], "register": ["/register"],
    "settings": ["/settings"], "editor": ["/editor", "/editor/:p"], "article detail": ["/article/:p"],
    "profile": ["/profile/:p"], "navbar": ["*"], "profile tab": ["/profile/:p"],
    "profile, article meta": ["/profile/:p", "/article/:p"], "article detail (variant)": ["/article/:p"],
    "login, register, settings": ["/login", "/register", "/settings"], "register, settings": ["/register", "/settings"],
    "login page heading": ["/login"], "register page heading": ["/register"], "home feed toggle": ["/"],
}
AUTHED_PAGES = {"/editor", "/editor/:p", "/settings"}

# From routing.md: "Delete article button (only shown to article's author)", "Delete comment button (only shown
# to comment's author)". The profile's "Edit Profile Settings" and the article's "Edit Article" follow the same rule.
OWNER_ONLY = {"Delete Article", "Edit Article", "Edit Profile Settings", "mod-options", "ion-trash-a"}


def _rid(prefix: str, text: str) -> str:
    return f"{prefix}-{hashlib.sha1(text.encode()).hexdigest()[:6]}"


def _tables(md: str) -> list[tuple[str, list[list[str]]]]:
    """(nearest heading, rows) for every markdown table."""
    out, heading, rows = [], "", []
    for line in md.splitlines():
        if line.startswith("#"):
            heading = line.strip("# ").strip()
        if line.startswith("|"):
            cells = [c.strip().strip("`") for c in line.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            rows.append(cells)
        elif rows:
            out.append((heading, rows[1:]))  # drop header row
            rows = []
    if rows:
        out.append((heading, rows[1:]))
    return out


def _pages(context: str) -> list[str]:
    return PAGE_TEMPLATES.get(context.strip().lower(), ["*"])


def _needs(pages: list[str], evidence_key: str) -> str:
    if any(k in evidence_key for k in OWNER_ONLY):
        return "owner"
    if pages and all(p in AUTHED_PAGES for p in pages):
        return "authed"
    return "anon"


def from_selectors(md: str) -> list[Requirement]:
    reqs: list[Requirement] = []
    src = "specs/e2e/SELECTORS.md"
    for heading, rows in _tables(md):
        for row in rows:
            if heading.startswith("Form Inputs"):
                sel, page = row[0], row[1]
                pages = [t for p in page.split(",") for t in _pages(p)]
                name = re.search(r'name="([^"]+)"', sel)
                ph = re.search(r'placeholder="([^"]+)"', sel)
                ev = Evidence(name_attrs=[name.group(1)] if name else [], placeholders=[ph.group(1)] if ph else [])
                stmt = f"{page} page has a '{(name or ph).group(1)}' field"
                reqs.append(Requirement(id=_rid("SEL", sel + page), statement=stmt, source=f"{src}#form-inputs",
                                        pages=pages, needs=_needs(pages, stmt), evidence=ev))
            elif len(row) == 3 and row[0].startswith("."):
                cls, tag, purpose = row
                stmt = f"{purpose} ({tag}{cls})"
                reqs.append(Requirement(id=_rid("CLS", cls + purpose), statement=stmt, source=f"{src}#css-classes",
                                        pages=["*"], needs=_needs(["*"], cls), evidence=Evidence(classes=[cls.lstrip(".")])))
            elif heading in ("Buttons", "Headings & Links") and len(row) >= 3:
                text, tag, ctx = row
                pages = _pages(ctx)
                texts = [t.strip().strip("`") for t in text.split("/")]
                stmt = f"{ctx}: '{text}' {tag}"
                reqs.append(Requirement(id=_rid("SEL", stmt), statement=stmt, source=f"{src}#required-text",
                                        pages=pages, needs=_needs(pages, text), evidence=Evidence(texts=texts)))
            elif heading == "Pages" and len(row) >= 2:
                route, page = row
                tpl = re.sub(r":\w+", ":p", route.split("?")[0]) or "/"
                stmt = f"Route {route} shows {page}"
                reqs.append(Requirement(id=_rid("RTE", route), statement=stmt, source=f"{src}#routes", pages=[tpl],
                                        needs="authed" if tpl in AUTHED_PAGES else "anon", kind="route"))
            elif heading.startswith("API Endpoints") and len(row) >= 2:
                ep = re.sub(r":\w+", ":p", row[0].rstrip("*"))
                for method in [m.strip() for m in row[1].split("(")[0].split(",")]:
                    stmt = f"App calls {method} {ep}"
                    reqs.append(Requirement(id=_rid("API", stmt), statement=stmt, source=f"{src}#api-endpoints",
                                            pages=["*"], kind="behaviour",
                                            needs=_api_needs(method, ep),
                                            evidence=Evidence(api=[f"{method} {ep}"])))
    return reqs


def _api_needs(method: str, ep: str) -> str:
    if method == "GET" or ep in ("/api/users", "/api/users/login"):
        return "anon"
    if method in ("PUT", "DELETE") and ep.startswith("/api/articles/:p") and "favorite" not in ep:
        return "owner"   # only the author edits/deletes an article or comment
    return "authed"


SYSTEM = """You turn a web app's product spec into testable requirements.
For each bullet in the routing spec, write one requirement:
- statement: one plain sentence.
- pages: route templates where it should be visible, using ':p' for parameters (e.g. /article/:p).
- needs: the minimum viewer state to see it: 'anon' (anyone), 'authed' (logged in), 'owner' (viewer owns the content).
- kind: 'element' if a visible UI element proves it, 'behaviour' if only an action or rendering proves it, 'route' for a page.
- evidence: the exact visible texts, input name attributes, placeholders, CSS classes or API calls ("METHOD /api/path/:p")
  that would prove it, copied from the templates spec. Leave a list empty rather than guess.
Only use facts in the spec."""


def from_routing(routing_md: str, templates_md: str, use_cache: bool = True) -> list[Requirement]:
    src = "docs/specifications/frontend/routing.md"
    bullets = [b.strip()[2:] for b in routing_md.splitlines() if b.strip().startswith("- ")]
    if not llm.available():
        reqs, page = [], ["*"]
        for line in routing_md.splitlines():
            if line.startswith("- "):
                urls = re.findall(r"`(/[^`]*)`", line)
                page = [re.sub(r"article-slug-here|:\w+", ":p", u).rstrip("/") or "/" for u in urls] or ["*"]
            if line.strip().startswith("- "):
                text = line.strip()[2:]
                reqs.append(Requirement(id=_rid("RT", text), statement=text, source=src, pages=page,
                                        kind="behaviour", needs="owner" if "author" in text else "anon"))
        return reqs
    prompt = f"<routing_spec>\n{routing_md}\n</routing_spec>\n\n<templates_spec>\n{templates_md}\n</templates_spec>\n\n" \
             f"Write exactly one requirement per routing bullet ({len(bullets)} bullets), in order."
    out = llm.structured(SYSTEM, prompt, SpecRequirements, use_cache=use_cache, max_tokens=6000)
    for r in out.requirements:
        r.id = _rid("RT", r.statement)
        r.source = src
        r.pages = [re.sub(r":\w+", ":p", p) for p in r.pages]
    return out.requirements


def ingest(use_cache: bool = True) -> list[Requirement]:
    d = SETTINGS.spec_dir
    reqs = from_selectors((d / "selectors.md").read_text())
    reqs += from_routing((d / "routing.md").read_text(), (d / "templates.md").read_text(), use_cache=use_cache)
    unique = {r.id: r for r in reqs}
    return list(unique.values())
