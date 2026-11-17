"""Stage 4 · Link the three layers.

Code <-> UI  (deterministic, scored)
  Screen     -> page component(s): crawl template == code route template
  Element    -> TemplateElement: candidates are only the template elements of components rendered on that
                screen; scored on name attr, placeholder, text, routerLink, tag, classes, and an independent
                channel: the API call the element made in the dry-run click vs the endpoint its handler reaches.
  Method     -> TemplateElement: event bindings, self-calls, and state the method writes that the template reads.

Requirement <-> UI (deterministic evidence match; LLM judge only for evidence-free prose requirements)
  covered : evidence found on a crawled screen of the right page
  absent  : the page was crawled in a state that should show it, nothing matched, and the code has nothing either
  unknown : never reached in the needed state, or present in code behind a condition the crawl didn't meet
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from pathlib import Path

from pydantic import BaseModel

from . import llm
from .config import ROOT, SETTINGS
from .models import CodeMap, Component, Crawl, Element, Flow, FlowNames, Gap, Link, Requirement, TemplateElement

STATE_ORDER = {"anon": 0, "authed": 1, "owner": 2}
WRITERS = {"set", "update", "patchValue", "reset", "setValue", "next"}


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (s or "").lower())).strip()


class Index:
    """Lookups over the code map."""

    def __init__(self, cm: CodeMap):
        self.cm = cm
        self.by_name = {c.name: c for c in cm.components}
        self.by_selector = {c.selector.lower(): c for c in cm.components if c.selector and not c.selector.startswith("[")}
        self.directives = {c.selector.strip("[]").lower(): c for c in cm.components if c.kind == "directive" and c.selector}
        self.tel_by_component: dict[str, list[TemplateElement]] = defaultdict(list)
        for t in cm.template_elements:
            self.tel_by_component[t.component].append(t)
        # The classes on each component's outermost element(s): where its output sits in the page's DOM.
        self.root_classes: dict[str, set[str]] = {
            c: {cls for t in tels if t.parent is None for cls in t.classes} - {"container", "row", "col-md-12"}
            for c, tels in self.tel_by_component.items()}

    def rendered(self, root: str) -> set[str]:
        """Component plus everything it renders, transitively."""
        seen, todo = set(), deque([root])
        while todo:
            c = todo.popleft()
            if c in seen or c not in self.by_name:
                continue
            seen.add(c)
            for t in self.tel_by_component[c]:
                if t.child_selector and t.child_selector in self.by_selector:
                    todo.append(self.by_selector[t.child_selector].name)
        return seen

    def components_for_template(self, template: str) -> set[str]:
        out = set()
        for r in self.cm.routes:
            if r.template == template and r.component:
                out |= self.rendered(r.component)
        if out:
            for lay in self.cm.layout_components:
                out |= self.rendered(lay)
        return out

    def endpoints_of_method(self, comp: Component, method: str, _seen=None) -> set[str]:
        """Endpoints a component method reaches through its own calls and injected services."""
        _seen = _seen or set()
        if (comp.name, method) in _seen:
            return set()
        _seen.add((comp.name, method))
        m = next((x for x in comp.methods if x.name == method), None)
        if not m:
            return set()
        eps = set()
        for call in m.calls_service:
            field, fn = call.split(".", 1)
            svc = self.by_name.get(comp.injects.get(field, ""))
            if svc and fn in svc.endpoints:
                eps.add(svc.endpoints[fn])
        for s in m.calls_self:
            eps |= self.endpoints_of_method(comp, s, _seen)
        return eps

    def methods_reaching(self, comp: Component, method: str) -> set[str]:
        """Methods that call `method` (transitively), including itself: a change in it affects all their bindings."""
        out = {method}
        changed = True
        while changed:
            changed = False
            for m in comp.methods:
                if m.name not in out and set(m.calls_self) & out:
                    out.add(m.name)
                    changed = True
        return out

    def state_written(self, comp: Component, method: str) -> set[str]:
        m = next((x for x in comp.methods if x.name == method), None)
        if not m:
            return set()
        return {c.split(".")[0] for c in m.calls_service if c.split(".")[-1] in WRITERS}

    def elements_bound_to(self, comp: Component, method: str) -> list[tuple[TemplateElement, str]]:
        """Template elements a method affects, with why. Lifecycle methods affect the whole template."""
        out = []
        callers = self.methods_reaching(comp, method)
        written = self.state_written(comp, method)
        for t in self.tel_by_component[comp.name]:
            if set(t.handlers) & callers:
                out.append((t, f"bound to {comp.name}.{'/'.join(sorted(set(t.handlers) & callers))}()"))
            elif written & set(t.identifiers):
                out.append((t, f"displays state written by {comp.name}.{method}() ({', '.join(sorted(written & set(t.identifiers)))})"))
            elif method in comp.lifecycle and (t.handlers or t.name_attr or t.identifiers):
                out.append((t, f"rendered after {comp.name}.{method}() loads data"))
        return out


# ---------------- code <-> UI ----------------

def _score(el: Element, t: TemplateElement, comp: Component, idx: Index) -> tuple[float, list[str]]:
    s, why = 0.0, []
    if el.tag == t.tag:
        s += 0.15
    elif not (el.tag in ("a", "button") and t.tag in ("a", "button")):
        return 0.0, []
    if el.name_attr and el.name_attr == t.name_attr:
        s += 0.6; why.append(f"name='{el.name_attr}'")
    if el.placeholder and el.placeholder == t.placeholder:
        s += 0.6; why.append(f"placeholder='{el.placeholder}'")
    if t.text and not el.repeated:
        a, b = norm(el.label), norm(t.text)
        if a and a == b:
            s += 0.5; why.append(f"text='{t.text}'")
        elif a and b and (a.startswith(b) or b in a):
            s += 0.3; why.append(f"text starts '{t.text}'")
    if el.href_template and t.router_link and el.href_template.rstrip("/") == ("/" + t.router_link.strip("/")).rstrip("/"):
        s += 0.4; why.append(f"link -> {el.href_template}")
    if el.classes and t.classes:
        j = len(set(el.classes) & set(t.classes)) / len(set(el.classes) | set(t.classes))
        if j:
            s += 0.2 * j; why.append(f"classes {j:.0%} overlap")
    roots = idx.root_classes.get(t.component, set())
    if roots:
        path_classes = set(re.findall(r"\.([\w-]+)", el.css_path))
        if roots & path_classes:
            s += 0.15; why.append(f"inside .{sorted(roots & path_classes)[0]}")
        else:
            s -= 0.1
    observed = {f"{c.method} {c.path}" for c in el.triggers if c.blocked}
    if observed and t.handlers:
        reach = set().union(*(idx.endpoints_of_method(comp, h) for h in t.handlers))
        if observed & reach:
            s += 0.5; why.append(f"dry-run call {sorted(observed & reach)[0]} matches handler")
        elif reach:
            s -= 0.3
    return min(s, 1.0), why


def _state_fit(t: TemplateElement, auth: str) -> float:
    """+0.15 if the element's @if conditions agree with the crawl's login state, -0.15 if they contradict it."""
    conds = " ".join(i[4:] for i in t.identifiers if i.startswith("@if:")).lower()
    if not conds:
        return 0.0
    says_anon = "unauthenticated" in conds or "not authenticated" in conds
    says_authed = "'authenticated'" in conds or ("authenticated" in conds and not says_anon)
    if "unavailable" in conds or "loading" in conds:
        return -0.15          # transient states the crawler never sits in
    if says_anon:
        return 0.15 if auth == "anon" else -0.15
    if says_authed:
        return 0.15 if auth != "anon" else -0.15
    return 0.0


def link_code_ui(crawl: Crawl, cm: CodeMap, idx: Index) -> tuple[list[Link], list[dict]]:
    links, review = [], []
    by_screen: dict[str, list[Element]] = defaultdict(list)
    for e in crawl.elements:
        by_screen[e.screen_id].append(e)
    for screen in crawl.screens:
        comps = idx.components_for_template(screen.template)
        for r in cm.routes:
            if r.template == screen.template:
                links.append(Link(src=screen.id, dst=r.component, kind="SERVED_BY", confidence=1.0,
                                  method="route", evidence=f"route {r.template} in {r.file}"))
        cands = [(t, idx.by_name[t.component]) for c in comps for t in idx.tel_by_component[c]]
        for el in by_screen[screen.id]:
            auth = screen.auth
            scored = []
            for t, comp in cands:
                sc, why = _score(el, t, comp, idx)
                if sc > 0 and (fit := _state_fit(t, auth)):
                    sc = min(1.0, sc + fit)
                    why = why + [f"@if matches {auth}" if fit > 0 else f"@if contradicts {auth}"]
                scored.append(((sc, why), t))
            scored.sort(key=lambda x: -x[0][0])
            if not scored or scored[0][0][0] < SETTINGS.link_review:
                continue
            (best, why), t = scored[0]
            second = scored[1][0][0] if len(scored) > 1 else 0.0
            t2 = scored[1][1] if len(scored) > 1 else None
            same_effect = t2 is not None and t2.component == t.component and t2.handlers == t.handlers \
                and t2.router_link == t.router_link
            if best - second < 0.1 and second >= SETTINGS.link_review and same_effect:
                # Two copies of the same control in one component (e.g. "Home" in two @if branches): which one
                # rendered doesn't change what a code change can break, so link both, no review needed.
                for (sc, w), tt in scored[:2]:
                    links.append(Link(src=tt.id, dst=el.id, kind="RENDERS", confidence=round(sc, 2),
                                      method="same-effect-tie", evidence="; ".join(w)))
                continue
            if best - second < 0.1 and second >= SETTINGS.link_review:
                # Two template elements explain this UI element equally well (e.g. the same "Home" link in
                # three @if branches). Keep both, discounted, and say so.
                for (sc, w), tt in scored[:2]:
                    links.append(Link(src=tt.id, dst=el.id, kind="RENDERS", confidence=round(sc * 0.8, 2),
                                      method="ambiguous", evidence="; ".join(w)))
                review.append({"element": el.id, "label": el.label, "screen": screen.id,
                               "candidates": [scored[0][1].id, scored[1][1].id], "why": "two equally good code matches"})
                continue
            links.append(Link(src=t.id, dst=el.id, kind="RENDERS", confidence=round(best, 2), method="scored",
                              evidence="; ".join(why)))
    return links, review


def link_methods(cm: CodeMap, idx: Index) -> list[Link]:
    out = []
    for comp in cm.components:
        if comp.kind not in ("component", "directive"):
            continue
        for m in comp.methods:
            for t, why in idx.elements_bound_to(comp, m.name):
                out.append(Link(src=f"{comp.name}.{m.name}", dst=t.id, kind="AFFECTS", confidence=1.0,
                                method="binding", evidence=why))
            for ep in idx.endpoints_of_method(comp, m.name):
                out.append(Link(src=f"{comp.name}.{m.name}", dst=ep, kind="CALLS_API", confidence=1.0, method="static"))
        if comp.kind == "directive":
            # A structural directive decides whether its host element AND everything inside it is rendered.
            key = comp.selector.strip("[]").lower()
            children: dict[str, list[TemplateElement]] = defaultdict(list)
            for t in cm.template_elements:
                if t.parent:
                    children[t.parent].append(t)
            for host in cm.template_elements:
                if key not in [d.lower() for d in host.directives]:
                    continue
                subtree, todo = [], [host]
                while todo:
                    t = todo.pop()
                    subtree.append(t)
                    todo += children[t.id]
                for t in subtree:
                    for m in comp.methods:
                        out.append(Link(src=f"{comp.name}.{m.name}", dst=t.id, kind="AFFECTS", confidence=0.9,
                                        method="directive",
                                        evidence=f"shown or hidden by *{comp.selector.strip('[]')} on {host.component} line {host.line}"))
    return out


# ---------------- requirements <-> UI ----------------

def _dom(screen) -> str:
    if not screen.dom:
        return ""
    p = ROOT / screen.dom
    return p.read_text() if p.exists() else ""


def _pieces(r: Requirement) -> list[tuple[str, str]]:
    ev = r.evidence
    return ([("text", t) for t in ev.texts] + [("name", n) for n in ev.name_attrs] +
            [("placeholder", p) for p in ev.placeholders] + [("class", c) for c in ev.classes] +
            [("api", a) for a in ev.api])


def _piece_on_element(p: tuple[str, str], el: Element) -> bool:
    kind, v = p
    if kind == "text":
        lab, t = norm(el.label), norm(v)
        return bool(t) and not el.repeated and (lab == t or lab.startswith(t + " "))
    if kind == "name":
        return el.name_attr == v
    if kind == "placeholder":
        return el.placeholder == v
    if kind == "class":
        return v in el.classes
    return v in {f"{c.method} {c.path}" for c in el.triggers}


def _visible_text(dom: str) -> str:
    dom = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", dom, flags=re.S)
    return norm(re.sub(r"<[^>]+>", " ", dom))


def _piece_in_dom(p: tuple[str, str], dom: str, text: str, screen) -> bool:
    kind, v = p
    if kind == "text":
        return bool(norm(v)) and f" {norm(v)} " in f" {text} "
    if kind == "name":
        return f'name="{v}"' in dom
    if kind == "placeholder":
        return f'placeholder="{v}"' in dom
    if kind == "class":
        return re.search(rf'class="[^"]*\b{re.escape(v)}\b', dom) is not None
    return v in {f"{c.method} {c.path}" for c in screen.on_load_calls}


def _matches(req: Requirement, el: Element) -> str | None:
    ev = req.evidence
    if ev.name_attrs and el.name_attr in ev.name_attrs:
        return f"input name='{el.name_attr}'"
    if ev.placeholders and el.placeholder in ev.placeholders:
        return f"placeholder '{el.placeholder}'"
    if ev.texts and not el.repeated:
        lab = norm(el.label)
        for t in ev.texts:
            if norm(t) and (lab == norm(t) or lab.startswith(norm(t) + " ")):
                return f"text '{el.label}'"
    if ev.api:
        calls = {f"{c.method} {c.path}" for c in el.triggers}
        if set(ev.api) & calls:
            return f"dry-run click called {sorted(set(ev.api) & calls)[0]}"
    return None


def _code_has(req: Requirement, cm: CodeMap, idx: Index) -> tuple[str, list[str]] | None:
    ev = req.evidence
    for t in cm.template_elements:
        hit = ((ev.name_attrs and t.name_attr in ev.name_attrs) or (ev.placeholders and t.placeholder in ev.placeholders)
               or (ev.texts and any(norm(x) and norm(t.text).startswith(norm(x)) for x in ev.texts))
               or (ev.classes and set(ev.classes) & set(t.classes)))
        if hit:
            conds = [i[4:] for i in t.identifiers if i.startswith("@if:")]
            return f"{t.component} ({t.file}:{t.line})", conds
    for c in cm.components:
        if c.kind == "service" and set(ev.api) & set(c.endpoints.values()):
            return f"{c.name} ({c.file})", []
    return None


class JudgeOut(BaseModel):
    verdict: str          # covered | absent | unclear
    element_ids: list[str]
    confidence: float
    reason: str


JUDGE = """You check whether a crawled web page satisfies a product requirement.
You get the requirement and a list of UI elements found on the relevant pages (id, screen, tag, label).
Answer covered (name the element ids that prove it), absent (the pages clearly lack it), or unclear
(it is a behaviour the element list can't show, like rendering or storage). Confidence 0-1. Be strict."""


def link_requirements(reqs: list[Requirement], crawl: Crawl, cm: CodeMap, idx: Index,
                      use_cache: bool = True) -> tuple[list[Link], list[Gap], list[dict]]:
    links, gaps, review = [], [], []
    screens = {s.id: s for s in crawl.screens}
    by_screen: dict[str, list[Element]] = defaultdict(list)
    for e in crawl.elements:
        by_screen[e.screen_id].append(e)
    reached = {(s.template, s.auth) for s in crawl.screens}

    for r in reqs:
        pages = {s.template for s in crawl.screens} if "*" in r.pages else set(r.pages)
        relevant = [s for s in crawl.screens if s.template in pages]
        eligible = [s for s in relevant if STATE_ORDER[s.auth] >= STATE_ORDER[r.needs]]
        found = False

        if r.kind == "route":
            for s in relevant:
                links.append(Link(src=r.id, dst=s.id, kind="COVERED_BY", confidence=1.0, method="route",
                                  evidence=f"crawled {s.sample_urls[0]}"))
                found = True
        # Evidence is checked piece by piece (each text, name, placeholder, class, API call), first against
        # crawled elements, then against the DOM snapshot. Confidence scales with the share of pieces found,
        # so "the page exists but half of what the spec describes is missing" is visible, not rounded up.
        pieces = _pieces(r)
        found_pieces: set[tuple] = set()
        req_links: list[Link] = []
        for s in relevant:
            dom = _dom(s)
            text = _visible_text(dom)
            for el in by_screen[s.id]:
                hits = [p for p in pieces if _piece_on_element(p, el)]
                if hits:
                    found_pieces |= set(hits)
                    # A shared CSS class alone (".btn-primary") is weak proof: many controls carry it.
                    class_only = all(k == "class" for k, _ in hits)
                    req_links.append(Link(src=r.id, dst=el.id, kind="COVERED_BY", confidence=0.4 if class_only else 0.95,
                                          method="class-only" if class_only else "evidence",
                                          evidence=", ".join(f"{k} '{v}'" for k, v in hits)))
            dom_hits = [p for p in pieces if p not in found_pieces and _piece_in_dom(p, dom, text, s)]
            if dom_hits:
                found_pieces |= set(dom_hits)
                req_links.append(Link(src=r.id, dst=s.id, kind="COVERED_BY", confidence=0.9, method="dom",
                                      evidence=", ".join(f"{k} '{v}'" for k, v in dom_hits) + " in page snapshot"))
        if req_links:
            frac = len(found_pieces) / len(pieces)
            for l in req_links:
                l.confidence = round(l.confidence if l.method == "class-only" else max(0.45, l.confidence * frac), 2)
            links.extend(req_links)
            found = True
            if frac < 1:
                missing = [f"{k} '{v}'" for k, v in pieces if (k, v) not in found_pieces]
                review.append({"requirement": r.id, "statement": r.statement,
                               "why": f"partly present ({len(found_pieces)}/{len(pieces)} pieces); missing {', '.join(missing[:4])}"})
                if frac < 0.5:
                    gaps.append(Gap(requirement_id=r.id, status="partial", searched_screens=[s.id for s in relevant],
                                    reason=f"only {len(found_pieces)}/{len(pieces)} expected pieces found; missing {', '.join(missing[:4])}"))

        empty_evidence = not any(r.evidence.model_dump().values()) and r.kind != "route"
        if not found and empty_evidence and llm.available() and relevant:
            cands = [e for s in relevant for e in by_screen[s.id] if e.label][:60]
            listing = "\n".join(f"{e.id} | {e.screen_id} | {e.tag} | {e.label}" for e in cands)
            j = llm.structured(JUDGE, f"Requirement: {r.statement}\nPages: {r.pages}\n\nElements:\n{listing}",
                               JudgeOut, use_cache=use_cache, max_tokens=800)
            valid = [i for i in j.element_ids if i in {e.id for e in cands}]   # never trust ids the model invented
            if j.verdict == "covered" and valid:
                for i in valid:
                    links.append(Link(src=r.id, dst=i, kind="COVERED_BY", confidence=round(min(j.confidence, 0.8), 2),
                                      method="llm-judge", evidence=j.reason))
                found = True
                if j.confidence < SETTINGS.link_accept:
                    review.append({"requirement": r.id, "statement": r.statement, "why": f"LLM judge unsure: {j.reason}"})
        if found:
            continue

        code = _code_has(r, cm, idx)
        searched = [s.id for s in eligible]
        if empty_evidence:
            gaps.append(Gap(requirement_id=r.id, status="unknown", searched_screens=searched,
                            reason="spec gives no checkable UI evidence (behaviour); needs a flow test or a human"))
            review.append({"requirement": r.id, "statement": r.statement, "why": "no UI evidence to check"})
        elif code and (r.needs == "owner" or not eligible or code[1]):
            cond = f" under condition '{code[1][0]}'" if code[1] else ""
            gaps.append(Gap(requirement_id=r.id, status="unknown", searched_screens=searched,
                            reason=f"in code at {code[0]}{cond}, but never shown in crawled states "
                                   f"({', '.join(sorted({s.auth for s in relevant})) or 'page not reached'}); needs '{r.needs}' state"))
        elif code and r.evidence.api:
            # The code can make this call, but the UI state that triggers it (an article you favourited, a user
            # you follow) only exists after a write, and the crawler's dry run blocks writes. Not evidence of absence.
            gaps.append(Gap(requirement_id=r.id, status="unknown", searched_screens=searched,
                            reason=f"in code at {code[0]}; the state that triggers it needs a prior write, "
                                   "which the dry-run crawl blocks"))
        elif eligible:
            gaps.append(Gap(requirement_id=r.id, status="absent", searched_screens=searched,
                            reason=f"searched {len(searched)} screen(s) in state >= {r.needs}; no match"
                                   + (f"; code has it at {code[0]}" if code else "; not found in code either")))
        else:
            missing = sorted(p for p in pages if not any((p, a) in reached for a in ("anon", "authed")))
            gaps.append(Gap(requirement_id=r.id, status="unknown", searched_screens=searched,
                            reason=f"never reached {', '.join(missing) or 'these pages'} as '{r.needs}'"))
    return links, gaps, review


# ---------------- flows ----------------

AUTH_WORDS = {"anon": "logged out", "authed": "logged in", "owner": "as the owner"}


def _describe(step: str, elements: dict[str, Element], screens: dict, tels: dict[str, TemplateElement]) -> str:
    if step in screens:
        s = screens[step]
        return f"on {s.template} ({AUTH_WORDS[s.auth]})"
    if step.startswith("route:"):
        return f"on {step[6:]} (only known from code)"
    if step in elements:
        e = elements[step]
        verb = "fill" if e.tag in ("input", "textarea") else "click"
        if e.repeated:
            example = re.search(r"e\.g\. '(.*)'$", e.label)
            return f"{verb} one of a list of {e.tag} items" + (f" (e.g. '{example.group(1)}')" if example and example.group(1) else "")
        return f"{verb} '{e.label or e.placeholder or e.name_attr or e.tag}'"
    if step in tels:
        t = tels[step]
        verb = "fill" if t.tag in ("input", "textarea") else "click"
        return f"{verb} '{t.text or t.placeholder or t.name_attr or t.tag}' (from code)"
    return step


def discover_flows(crawl: Crawl, cm: CodeMap, idx: Index, use_cache: bool = True) -> list[Flow]:
    """User flows = journeys that end in an action that changes data.

    Observed: shortest click path from the home page to the action's screen, the screen's inputs, then the action.
      Logged-in journeys start with the observed sign-in journey, so each one is complete from a cold start.
    Stitched from code: when the crawl never reached the action's page (e.g. /editor/:slug, which needs an article
      you own), the journey runs through the crawl as far as it got, then continues through the link in the code
      that leads to that page (e.g. the article page's 'Edit Article' link, shown only to the author).
    One flow per (page, API call). Names come from the LLM, checked; a plain name is used otherwise.
    """
    by_screen: dict[str, list[Element]] = defaultdict(list)
    for e in crawl.elements:
        by_screen[e.screen_id].append(e)
    screens = {s.id: s for s in crawl.screens}
    elements = {e.id: e for e in crawl.elements}
    tels = {t.id: t for t in cm.template_elements}

    paths: dict[str, dict[str, list[str]]] = {}
    for auth in ("anon", "authed"):
        start = f"/|{auth}"
        prev: dict[str, tuple[str, str] | None] = {start: None}
        q = deque([start])
        while q:
            cur = q.popleft()
            for t in crawl.transitions:
                nxt = f"{t.to_template}|{auth}"
                if t.from_screen == cur and nxt in screens and nxt not in prev:
                    prev[nxt] = (cur, t.element_id)
                    q.append(nxt)
        paths[auth] = {}
        for sid in prev:
            path, cur = [sid], sid
            while prev.get(cur):
                p, via = prev[cur]
                path = [p, via] + path
                cur = p
            paths[auth][sid] = path

    def action_steps(sid: str, el: Element) -> list[str]:
        # Only the inputs in the same <form> as the action: the comment box isn't part of "Follow".
        m = re.search(r"^(.*?\bform\b[^>]*)", el.css_path)
        inputs = [e.id for e in by_screen[sid] if e.tag in ("input", "textarea") and not e.repeated
                  and m and e.css_path.startswith(m.group(1))]
        return paths[sid.split("|")[1]][sid] + inputs + [el.id]

    flows: dict[tuple, Flow] = {}
    login: list[str] = []
    for sid, els in by_screen.items():
        if sid not in paths[sid.split("|")[1]]:
            continue
        for el in els:
            writes = [f"{c.method} {c.path}" for c in el.triggers if c.blocked]
            if writes and writes[0] == "POST /api/users/login" and not login:
                login = action_steps(sid, el)
    for sid, els in sorted(by_screen.items()):
        auth = sid.split("|")[1]
        if sid not in paths[auth]:
            continue
        for el in els:
            writes = [f"{c.method} {c.path}" for c in el.triggers if c.blocked]
            if not writes:
                continue
            key = (screens[sid].template, writes[0])
            if key in flows:
                continue
            steps = (login if auth == "authed" else []) + action_steps(sid, el)
            flows[key] = Flow(id=f"flow:{screens[sid].template}:{writes[0]}", name="", steps=steps,
                              action_element=el.id, calls=writes, requires_login=auth == "authed")

    # Stitched from code: pages never reached, whose components have handlers that write.
    reached = {s.template for s in crawl.screens}
    for r in cm.routes:
        if r.template in reached or not r.component:
            continue
        comp = idx.by_name[r.component]
        entry: list[str] = []
        for t in cm.template_elements:   # a link in the code that leads here, on a page the crawl did reach
            if not t.router_link or "/" + t.router_link.strip("/") != r.template:
                continue
            for host in cm.routes:
                sid = f"{host.template}|authed"
                if t.component in idx.components_for_template(host.template) and sid in paths["authed"]:
                    entry = login + paths["authed"][sid] + [t.id]
                    break
            if entry:
                break
        for t in idx.tel_by_component[comp.name]:
            eps = sorted({e for h in t.handlers for e in idx.endpoints_of_method(comp, h) if not e.startswith("GET")})
            if not eps:
                continue
            inputs = [x.id for x in idx.tel_by_component[comp.name] if x.tag in ("input", "textarea")]
            flows[(r.template, eps[0])] = Flow(
                id=f"flow:code:{r.template}:{t.handlers[0]}", name="", observed=False, requires_login=r.guarded,
                steps=(entry or (login if r.guarded else [])) + [f"route:{r.template}"] + inputs + [t.id],
                action_element=t.id, calls=eps)

    out = list(flows.values())
    for f in out:
        f.narrative = [_describe(s, elements, screens, tels) for s in f.steps]
    _name_flows(out, use_cache)
    return out


NAMER = """You name user journeys in a web app for a QA lead. For each journey you get its steps and the API
calls its last action makes. Return, for every id, a name (3-8 words, imperative, product language, e.g.
"Publish a new article") and a one-sentence goal. If a journey is marked 'from code', its last steps were never
seen in the running app: the name stays the same, the goal says so. Use only what the steps show."""


def _plain_name(f: Flow) -> str:
    last = f.narrative[-1] if f.narrative else f.action_element
    page = next((n for n in reversed(f.narrative) if n.startswith("on ")), "")
    return f"{last.replace(chr(39), '')} {page}".strip()


def _name_flows(flows: list[Flow], use_cache: bool) -> None:
    for f in flows:
        f.name = _plain_name(f)
    if not llm.available() or not flows:
        return
    listing = "\n\n".join(f"id: {f.id}\nfrom code: {not f.observed}\ncalls: {', '.join(f.calls)}\nsteps:\n- "
                          + "\n- ".join(f.narrative) for f in flows)
    out = llm.structured(NAMER, listing, FlowNames, use_cache=use_cache, max_tokens=3000)
    # Check each name before trusting it: for a real flow id, short, and not shared with another flow.
    # A rejected name falls back to the plain one; the others still apply.
    counts = defaultdict(int)
    for n in out.flows:
        counts[n.name.lower()] += 1
    names = {n.id: n for n in out.flows if counts[n.name.lower()] == 1 and len(n.name.split()) <= 10}
    for f in flows:
        if f.id in names:
            f.name, f.goal = names[f.id].name, names[f.id].goal
