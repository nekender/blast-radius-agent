"""Stage 2b · Ingest the test suite (deterministic).

RealWorld ships a Playwright E2E suite (data/spec/e2e, 12 spec files) written against the selectors contract.
Each `test(...)` becomes a Test node. Its body is read as an ordered list of steps, "on page X, act on
selector Y", with helper calls (createArticle, editArticle, login, ...) expanded inline and the current page
tracked through goto / waitForURL / toHaveURL. Each selector is then resolved to crawled elements on that page.

This adds the layer Testsigma's model is built on:
  (Requirement)-[:TESTED_BY]->(Test)-[:USES]->(Element)
so the blast radius can say which tests to re-run, which requirements lose test coverage, and which
requirements and controls no test touches at all.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

from .code_map import match_close
from .config import SETTINGS
from .models import Crawl, Element, Link, Requirement

STEP_RE = re.compile(
    r"\b(?:page|\w+)\.(goto|fill|click|press|locator|waitForSelector|waitForURL|check|selectOption|getByText)\(\s*"
    r"(`[^`]*`|'[^']*'|\"[^\"]*\"|/(?:\\/|[^/\n])+/[a-z]*)"
    r"|expect\(page\)\.(?:not\.)?toHaveURL\(\s*(`[^`]*`|'[^']*'|\"[^\"]*\"|/(?:\\/|[^/\n])+/[a-z]*)"
    r"|\b(\w+)\(\s*page\b")


class Step(BaseModel):
    page: str | None          # route template the step happens on, if known
    action: str               # fill / click / press / locator / waitForSelector / goto
    selector: str = ""


class Test(BaseModel):
    id: str
    name: str
    file: str
    line: int
    steps: list[Step] = Field(default_factory=list)


def _lit(tok: str) -> str:
    return tok[1:-1] if tok[:1] in "`'\"" else tok


def _route(tok: str) -> str | None:
    """'/editor', `/editor/${slug}`, /\\/article\\/.+/  ->  a path or path pattern (':p' for parameters)."""
    if tok[:1] == "/" and tok.count("/") >= 2 and "\\/" in tok:          # a regex literal
        s = tok[1:tok.rindex("/")].replace("\\/", "/").lstrip("^").rstrip("$")
        s = re.sub(r"\.\+|\[\^/\]\+|\\d\+|\.\*", ":p", s)
    else:
        s = _lit(tok)
    s = re.sub(r"\$\{[^}]+\}", ":p", s).split("?")[0].split("#")[0]
    s = re.sub(r"/[^/]*:p[^/]*", "/:p", s)
    if not s.startswith("/") or "*" in s or "\\" in s:
        return None
    return s.rstrip("/") or "/"


def to_template(path: str | None, templates: list[str]) -> str | None:
    """Map a concrete or pattern path to a known route template ('/article/test-article' -> '/article/:p')."""
    if path is None:
        return None
    segs = [x for x in path.split("/") if x]
    for t in sorted(templates, key=lambda t: t.count(":p")):   # prefer the most literal template
        ts = [x for x in t.split("/") if x]
        if len(ts) == len(segs) and all(a == b or b == ":p" for a, b in zip(segs, ts)):
            return t
    return None


def _functions(text: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"export\s+async\s+function\s+(\w+)\s*\(", text):
        start = text.index("{", match_close(text, m.end() - 1, "(", ")"))
        out[m.group(1)] = text[start:match_close(text, start) + 1]
    return out


def _steps(body: str, helpers: dict[str, str], page: str | None = None, depth: int = 0) -> tuple[list[Step], str | None]:
    steps: list[Step] = []
    pending: str | None = None   # Promise.all([waitForURL(x), click(...)]): the page changes AFTER the click
    for m in STEP_RE.finditer(body):
        if m.group(1):
            action, tok = m.group(1), m.group(2)
            if action == "goto":
                page, pending = _route(tok) or page, None
                steps.append(Step(page=page, action="goto", selector=""))
            elif action == "waitForURL":
                pending = _route(tok) or pending
            else:
                sel = _lit(tok)
                if action == "getByText":
                    action, sel = "locator", f"text={sel}"
                # Locators held in variables (ownComment.locator, page2.locator) act on the current page; a second
                # browser page (page2) belongs to another user's session, so its page is unknown.
                on = None if re.fullmatch(r"page\d+", m.group(0).split(".")[0]) else page
                steps.append(Step(page=on, action=action, selector=sel))
                if pending and action == "click":
                    page, pending = pending, None
                elif action == "click" and (h := re.search(r'\[href="([^"]+)"\]', sel)):
                    page = _route(h.group(1)) or page      # clicking a link moves to its page
        elif m.group(3):
            page, pending = _route(m.group(3)) or page, None
        elif m.group(4) in helpers and depth < 3:
            if pending:
                page, pending = pending, None
            sub, page = _steps(helpers[m.group(4)], helpers, page, depth + 1)
            steps += sub
    return steps, (pending or page)


def parse_suite(root: Path = SETTINGS.spec_dir / "e2e") -> list[Test]:
    helpers: dict[str, str] = {}
    for f in sorted((root / "helpers").glob("*.ts")):
        helpers.update(_functions(f.read_text()))
    tests: list[Test] = []
    for f in sorted(root.glob("*.spec.ts")):
        text = f.read_text()
        for m in re.finditer(r"\btest\(\s*(['\"`])(.+?)\1\s*,\s*async", text):
            start = text.index("{", text.index("=>", m.end()))
            body = text[start:match_close(text, start) + 1]
            line = text.count("\n", 0, m.start()) + 1
            steps, _ = _steps(body, helpers)
            tests.append(Test(id=f"T:{f.name}:{line}", name=m.group(2), file=f"data/spec/e2e/{f.name}", line=line, steps=steps))
    return tests


# ---------------- selector -> elements ----------------

def _last_compound(sel: str) -> str:
    """'nav.navbar a[href="/login"]' -> 'a[href="/login"]'; spaces inside [...] or (...) don't split."""
    depth, start = 0, 0
    for i, c in enumerate(sel):
        if c in "[(":
            depth += 1
        elif c in "])":
            depth -= 1
        elif depth == 0 and c in " >":
            start = i + 1
    return sel[start:].strip()


def selector_pieces(sel: str) -> dict:
    """input[name="title"] / button:has-text("Publish Article") / a[href="/editor"] / .article-preview"""
    p: dict = {"tag": None, "name": None, "placeholder": None, "text": None, "href": None, "type": None, "classes": []}
    sel = sel.strip()
    if sel.startswith("text="):
        p["text"] = sel[5:].strip("'\"")
        return p
    last = _last_compound(sel)   # the element itself, not its ancestors
    if (m := re.match(r"^([a-z][a-z0-9]*)", last)):
        p["tag"] = m.group(1)
    p["name"] = (re.search(r'\[name="([^"]+)"\]', last) or [None, None])[1]
    p["placeholder"] = (re.search(r'\[placeholder="([^"]+)"\]', last) or [None, None])[1]
    p["text"] = (re.search(r':has-text\(["\']([^"\']+)["\']\)', last) or re.search(r':text\(["\']([^"\']+)["\']\)', last) or [None, None])[1]
    p["type"] = (re.search(r'\[type="([^"]+)"\]', last) or [None, None])[1]
    if (h := re.search(r'\[href="([^"]+)"\]', last)):
        p["href"] = _route(h.group(1))
    p["classes"] = re.findall(r"\.([\w-]+)", re.sub(r"\[[^\]]*\]|:has-text\([^)]*\)", "", last))
    # classes anywhere in the selector, ancestors included: 'span.mod-options i.ion-trash-a' tests both
    p["all_classes"] = re.findall(r"\.([\w-]+)", re.sub(r"\[[^\]]*\]|:has-text\([^)]*\)|:text\([^)]*\)", "", sel))
    return p


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def element_matches(p: dict, e: Element) -> float:
    """0 = no; 0.9 = an identifying attribute matches; 0.5 = only tag/classes match (weak)."""
    if p["tag"] and p["tag"] != e.tag:
        return 0.0
    strong = False
    if p["name"]:
        if e.name_attr != p["name"]:
            return 0.0
        strong = True
    if p["placeholder"]:
        if e.placeholder != p["placeholder"]:
            return 0.0
        strong = True
    if p["text"]:
        if e.repeated or _norm(p["text"]) not in _norm(e.label):
            return 0.0
        strong = True
    if p["href"]:
        if (e.href_template or "").rstrip("/") != p["href"].rstrip("/"):
            return 0.0
        strong = True
    if p["type"]:
        if e.input_type != p["type"]:
            return 0.0
        strong = True   # button[type="submit"]: identifying within the page's form
    if p["classes"] and not set(p["classes"]) <= set(e.classes):
        return 0.0
    if not strong and not p["classes"]:
        return 0.0
    return 0.9 if strong else 0.5


def link_tests(tests: list[Test], crawl: Crawl, reqs: list[Requirement], req_links: list[Link]) -> tuple[list[Link], list[Link], dict]:
    """(Test)-[:USES]->(Element), (Requirement)-[:TESTED_BY]->(Test), and coverage numbers."""
    by_tpl: dict[str, list[Element]] = defaultdict(list)
    screen_tpl = {s.id: s.template for s in crawl.screens}
    templates = sorted(set(screen_tpl.values()))
    for t in tests:
        for st in t.steps:
            st.page = to_template(st.page, templates) if st.page else None
    for e in crawl.elements:
        by_tpl[screen_tpl.get(e.screen_id, "")].append(e)
    uses: dict[tuple, Link] = {}
    for t in tests:
        for st in t.steps:
            if st.action == "goto" or not st.selector:
                continue
            p = selector_pieces(st.selector)
            pool = by_tpl.get(st.page, []) if st.page else crawl.elements
            for e in pool:
                c = element_matches(p, e)
                if c and ((t.id, e.id) not in uses or uses[(t.id, e.id)].confidence < c):
                    uses[(t.id, e.id)] = Link(src=t.id, dst=e.id, kind="USES", confidence=c, method=st.action,
                                              evidence=f"{st.action} '{st.selector}' on {st.page or 'any page'}")
    used_by: dict[str, set[str]] = defaultdict(set)
    for (tid, eid), l in uses.items():
        if l.confidence >= SETTINGS.link_review:
            used_by[eid].add(tid)
    tested: dict[tuple, Link] = {}
    for l in req_links:   # a requirement is tested by every test that uses an element proving it
        if l.confidence < SETTINGS.link_review:
            continue
        for tid in used_by.get(l.dst, ()):
            if (l.src, tid) not in tested:
                tested[(l.src, tid)] = Link(src=l.src, dst=tid, kind="TESTED_BY", confidence=round(min(l.confidence, 0.9), 2),
                                            method="via-element", evidence=f"test uses {l.dst}, which proves it")
    # A test that opens a page tests what that page shows or loads (requirements proven by the screen itself).
    screen_reqs: dict[str, list[Link]] = defaultdict(list)
    for l in req_links:
        if l.dst in screen_tpl and l.confidence >= SETTINGS.link_review:
            screen_reqs[screen_tpl[l.dst]].append(l)
    for t in tests:
        for tpl in {st.page for st in t.steps if st.page}:
            for l in screen_reqs.get(tpl, []):
                conf = 0.5 if l.evidence.startswith("class ") else 0.6   # a page that merely carries a CSS class
                if (l.src, t.id) not in tested or tested[(l.src, t.id)].confidence < conf:
                    tested[(l.src, t.id)] = Link(src=l.src, dst=t.id, kind="TESTED_BY", confidence=conf,
                                                 method="visits-page", evidence=f"test is on {tpl}, which {l.evidence}")

    # Direct links: the test's own selectors name the requirement's evidence on the right page, or it visits the
    # page a route requirement is about. Class-only matches are weak, as elsewhere.
    for t in tests:
        for st in t.steps:
            p = selector_pieces(st.selector) if st.selector else None
            for r in reqs:
                pages_ok = "*" in r.pages or st.page is None or st.page in r.pages
                if not pages_ok:
                    continue
                conf, why = 0.0, ""
                if r.kind == "route" and st.action == "goto" and st.page in r.pages:
                    conf, why = 0.7, f"opens {st.page}"
                elif p:
                    ev = r.evidence
                    if (p["name"] and p["name"] in ev.name_attrs) or (p["placeholder"] and p["placeholder"] in ev.placeholders) \
                            or (p["text"] and any(_norm(p["text"]) == _norm(x) for x in ev.texts)):
                        conf, why = 0.85, f"{st.action} '{st.selector}'"
                    elif p.get("all_classes") and set(p["all_classes"]) & set(ev.classes):
                        conf, why = 0.5, f"{st.action} '{st.selector}' (class only)"
                if conf and ((r.id, t.id) not in tested or tested[(r.id, t.id)].confidence < conf):
                    tested[(r.id, t.id)] = Link(src=r.id, dst=t.id, kind="TESTED_BY", confidence=conf,
                                                method="selector-evidence", evidence=why)
    tested = {k: l for k, l in tested.items() if l.confidence >= SETTINGS.link_review}
    tested_reqs = {k[0] for k in tested}
    interactive = [e for e in crawl.elements if e.tag in ("a", "button", "input", "textarea") and not e.repeated]
    stats = {"tests": len(tests), "test_steps": sum(len(t.steps) for t in tests),
             "tests_linked": len({k[0] for k in uses}),
             "requirements_tested": len(tested_reqs), "requirements_untested": len(reqs) - len(tested_reqs),
             "controls_untested": sum(1 for e in interactive if e.id not in used_by), "controls_total": len(interactive)}
    return list(uses.values()), list(tested.values()), stats
