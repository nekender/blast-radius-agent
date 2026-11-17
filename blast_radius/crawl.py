"""Stage 1 · Crawl (deterministic).

A Playwright breadth-first crawler, run once logged out and once logged in.

Choices (see docs/design.md):
- No LLM in the loop. The crawl is the ground the rest of the graph stands on, so it must be
  reproducible run to run. Meaning is added later, by linking to the spec and the code.
- State identity = route template + auth state. Templates are learned from the URLs themselves:
  a path position with several distinct values (article slugs, usernames, tags) becomes ':p'.
- Each template is visited at most N times, so 500 articles don't become 500 screens.
- Dry-run clicks: every button is clicked with all non-GET API calls intercepted and aborted.
  We learn which API a button calls without writing anything to the live app.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from collections import defaultdict, deque
from urllib.parse import urljoin, urlparse

from playwright.async_api import Page, async_playwright

from .config import SETTINGS
from .models import ApiCall, AuthState, Crawl, Element, Screen, Transition

EXTRACT_JS = r"""
() => {
  const sel = 'a, button, input, textarea, select, [role=button], h1, h2, h3, nav, .error-messages';
  const cssPath = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && el.tagName !== 'BODY') {
      let p = el.tagName.toLowerCase();
      const cls = [...el.classList].filter(c => !c.startsWith('ng-')).slice(0, 2);
      if (cls.length) p += '.' + cls.join('.');
      const sib = el.parentElement ? [...el.parentElement.children].filter(s => s.tagName === el.tagName) : [];
      if (sib.length > 1) p += `:nth-of-type(${sib.indexOf(el) + 1})`;
      parts.unshift(p);
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  // Accessibility role and name: what a control IS and what it's CALLED, the way assistive tech sees it.
  const roleOf = (el) => {
    const r = el.getAttribute('role'); if (r) return r;
    const t = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (t === 'button') return 'button';
    if (t === 'textarea') return 'textbox';
    if (t === 'select') return 'combobox';
    if (t === 'nav') return 'navigation';
    if (/^h[1-6]$/.test(t)) return 'heading';
    if (t === 'ul' || t === 'ol') return 'list';
    if (t === 'input') return ({checkbox: 'checkbox', radio: 'radio', submit: 'button', button: 'button',
                                search: 'searchbox'})[type] || 'textbox';
    return null;
  };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const nameOf = (el) => {
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const by = el.getAttribute('aria-labelledby');
    if (by) return clean(by.split(' ').map(id => (document.getElementById(id) || {}).innerText || '').join(' '));
    if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) return clean(l.innerText); }
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')
      return clean(el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name'));
    return clean(el.innerText || el.getAttribute('title') || (el.querySelector('img') || {}).alt || el.value);
  };
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const visible = r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
    if (!visible) continue;
    out.push({
      tag: el.tagName.toLowerCase(), role: roleOf(el), label: el.tagName === 'NAV' ? '' : nameOf(el),
      name_attr: el.getAttribute('name'), placeholder: el.getAttribute('placeholder'),
      input_type: el.getAttribute('type'), href: el.getAttribute('href'),
      classes: [...el.classList].filter(c => !c.startsWith('ng-')), css_path: cssPath(el),
    });
  }
  return out;
}
"""

NTH = re.compile(r":nth-of-type\(\d+\)")


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


class Templater:
    """Learns route templates from every URL seen (visited or just linked)."""

    def __init__(self) -> None:
        self.values: dict[tuple, set[str]] = defaultdict(set)

    def observe(self, path: str) -> None:
        segs = [s for s in path.split("/") if s]
        for i, s in enumerate(segs[1:], start=1):
            self.values[(segs[0], i)].add(s)

    def template(self, path: str) -> str:
        segs = [s for s in path.split("/") if s]
        if not segs:
            return "/"
        out = [segs[0]]
        for i, s in enumerate(segs[1:], start=1):
            many = len(self.values[(segs[0], i)]) >= 2
            out.append(":p" if many or re.search(r"\d|-", s) and len(s) > 12 else s)
        return "/" + "/".join(out)


def template_api(path: str) -> str:
    """/api/articles/some-slug/comments/12 -> /api/articles/:p/comments/:p"""
    segs = [s for s in urlparse(path).path.split("/") if s]
    fixed = {"api", "articles", "comments", "favorite", "users", "login", "user", "profiles", "follow", "tags", "feed"}
    return "/" + "/".join(s if s in fixed else ":p" for s in segs)


class Crawler:
    def __init__(self, app_url: str = SETTINGS.app_url, max_pages: int = SETTINGS.max_pages):
        self.app_url = app_url.rstrip("/")
        self.host = urlparse(self.app_url).netloc
        self.max_pages = max_pages
        self.templater = Templater()
        self.screens: dict[str, Screen] = {}
        self.elements: dict[str, Element] = {}
        self.transitions: list[Transition] = []
        self.unreached: list[dict] = []
        self.calls: list[ApiCall] = []
        self.shots = SETTINGS.artifacts / "screens"
        self._a11y_sets: dict[str, list[set[str]]] = {}
        self.doms = SETTINGS.artifacts / "dom"

    # ----- account (the one write the crawler is allowed to make) -----

    async def _auth_state(self, browser, token: str) -> str:
        ctx = await browser.new_context()
        await ctx.add_init_script(f"localStorage.setItem('jwtToken', {json.dumps(token)});")
        page = await ctx.new_page()
        await page.goto(f"{self.app_url}/", wait_until="networkidle")
        state = await page.evaluate("window.__conduit_debug__ ? window.__conduit_debug__.getAuthState() : 'unknown'")
        await ctx.close()
        return state

    async def _submit(self, browser, path: str, fields: dict) -> str | None:
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(f"{self.app_url}{path}", wait_until="networkidle")
        for name, value in fields.items():
            await page.fill(f'input[name="{name}"]', value)
        await page.click('button[type="submit"]')
        try:
            await page.wait_for_function("() => !!localStorage.getItem('jwtToken')", timeout=15000)
            token = await page.evaluate("localStorage.getItem('jwtToken')")
        except Exception:  # noqa: BLE001
            token = None
        await ctx.close()
        return token

    async def ensure_account(self, browser) -> str:
        """A working session for the logged-in pass. The demo backend resets its data from time to time,
        so a saved token is checked first, then the saved login, and only then is a new account registered."""
        path = SETTINGS.state_dir / "account.json"
        account = json.loads(path.read_text()) if path.exists() else None
        if account and await self._auth_state(browser, account["token"]) == "authenticated":
            return account["token"]
        if account:
            token = await self._submit(browser, "/login", {"email": account["email"], "password": account["password"]})
            if token:
                account["token"] = token
                path.write_text(json.dumps(account, indent=2))
                return token
        user = f"br-test-{secrets.token_hex(3)}"
        account = {"username": user, "email": f"{user}@example.com", "password": secrets.token_urlsafe(12)}
        token = await self._submit(browser, "/register", {k: account[k] for k in ("username", "email", "password")})
        if not token:
            raise RuntimeError("could not sign up a test account on the demo app")
        account["token"] = token
        SETTINGS.state_dir.mkdir(exist_ok=True)
        path.write_text(json.dumps(account, indent=2))
        return token

    # ----- crawl -----

    async def run(self) -> Crawl:
        self.shots.mkdir(parents=True, exist_ok=True)
        self.doms.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            await self._crawl_state(browser, "anon", token=None)
            token = await self.ensure_account(browser)
            await self._crawl_state(browser, "authed", token=token)
            await browser.close()
        self._retemplate()
        return Crawl(app_url=self.app_url, screens=list(self.screens.values()),
                     elements=list(self.elements.values()), transitions=self.transitions,
                     unreached=self.unreached)

    async def _crawl_state(self, browser, auth: AuthState, token: str | None) -> None:
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        if token:
            # Re-applied on every navigation, so a dry-run click on "logout" can't end the session.
            await ctx.add_init_script(f"if (!localStorage.getItem('jwtToken')) localStorage.setItem('jwtToken', {json.dumps(token)});")
        await ctx.route("**/api/**", self._intercept)
        page = await ctx.new_page()

        queue: deque[str] = deque([f"{self.app_url}/"])
        seen_urls: set[str] = set()
        per_template: dict[str, int] = defaultdict(int)
        visited = 0
        while queue and visited < self.max_pages:
            url = queue.popleft()
            path = urlparse(url).path or "/"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            tpl = self.templater.template(path)
            if per_template[tpl] >= SETTINGS.max_instances_per_template:
                continue
            per_template[tpl] += 1
            visited += 1
            for link in await self._visit(page, url, tpl, auth):
                if link not in seen_urls:
                    queue.append(link)
        # Templates linked to but never visited in this state, and why
        visited_tpls = {s.template for s in self.screens.values() if s.auth == auth}
        for t in sorted({tr.to_template for tr in self.transitions} - visited_tpls):
            self.unreached.append({"template": t, "auth": auth, "reason": "linked but outside crawl budget"})
        await ctx.close()

    async def _intercept(self, route) -> None:
        req = route.request
        call = ApiCall(method=req.method, path=template_api(req.url), blocked=req.method not in ("GET", "OPTIONS"))
        self.calls.append(call)
        if call.blocked:
            await route.abort()   # dry run: never write to the live app
        else:
            await route.continue_()

    async def _visit(self, page: Page, url: str, tpl: str, auth: AuthState) -> list[str]:
        self.calls.clear()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:  # noqa: BLE001
            self.unreached.append({"template": tpl, "auth": auth, "reason": f"load failed: {e.__class__.__name__}"})
            return []
        landed = self.templater.template(urlparse(page.url).path or "/")
        if landed != tpl:
            # Route guard or app redirect: the requested page is not reachable in this state.
            self.unreached.append({"template": tpl, "auth": auth, "reason": f"redirected to {landed}"})
            return []
        if auth != "anon":
            state = await page.evaluate("window.__conduit_debug__ ? window.__conduit_debug__.getAuthState() : 'unknown'")
            if state != "authenticated":
                raise RuntimeError(f"logged-in pass is not logged in on {url} (auth state: {state}); not recording it")
        on_load = [c for c in self.calls if not c.blocked]
        raw = await page.evaluate(EXTRACT_JS)
        links: list[str] = []
        for r in raw:
            if r["href"] and not r["href"].startswith(("mailto:", "javascript:", "#")):
                full = urljoin(self.app_url + "/", r["href"])
                if urlparse(full).netloc == self.host:
                    self.templater.observe(urlparse(full).path)
                    links.append(full.split("#")[0])
        tpl = self.templater.template(urlparse(page.url).path or "/")
        screen_id = f"{tpl}|{auth}"
        a11y_lines = await self._a11y_lines(page)
        structure = _hash(*sorted(a11y_lines))
        screen = self.screens.get(screen_id)
        if screen is None:
            shot = f"{_hash(screen_id)}.png"
            await page.screenshot(path=str(self.shots / shot), full_page=True)
            dom = f"{_hash(screen_id)}.html"
            (self.doms / dom).write_text(await page.content())
            a11y = SETTINGS.artifacts / "a11y"
            a11y.mkdir(parents=True, exist_ok=True)
            (a11y / f"{_hash(screen_id)}.txt").write_text("\n".join(a11y_lines))
            screen = Screen(id=screen_id, template=tpl, auth=auth, sample_urls=[], title=await page.title(),
                            structure_hash=structure, on_load_calls=_dedupe(on_load), screenshot=f"artifacts/screens/{shot}",
                            dom=f"artifacts/dom/{dom}")
            screen.a11y_states = [structure]
            self.screens[screen_id] = screen
            self._a11y_sets[screen_id] = [set(a11y_lines)]
        else:
            # Same route and login state, different accessibility tree = a different UI state on one URL.
            # Near-duplicates (a list with 10 vs 11 rows) are the same state: Jaccard >= 0.9 on (role, name) lines.
            sets = self._a11y_sets.setdefault(screen_id, [])
            if all(len(set(a11y_lines) & x) / max(1, len(set(a11y_lines) | x)) < 0.9 for x in sets):
                sets.append(set(a11y_lines))
                screen.a11y_states.append(structure)
        screen.sample_urls.append(page.url)

        elements = self._to_elements(raw, screen_id)
        for el in elements:
            self.elements.setdefault(el.id, el)
            if el.href_template:
                self.transitions.append(Transition(from_screen=screen_id, element_id=el.id, to_template=el.href_template))
        if len(screen.sample_urls) == 1:
            await self._dry_run_clicks(page, url, screen_id, elements)
        return links

    def _to_elements(self, raw: list[dict], screen_id: str) -> list[Element]:
        # Group by structure AND identity attributes: three inputs in identical fieldsets are three fields,
        # but twenty article cards with the same structure are one data-driven element.
        # Links to fixed pages (Home, Sign in, Settings) are distinct controls even when their markup is identical;
        # links to parameterised pages (/article/:p) are the same control repeated over data.
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for r in raw:
            target = None
            if r["href"] and not r["href"].startswith(("mailto:", "javascript:", "#")):
                u = urlparse(urljoin(self.app_url + "/", r["href"]))
                target = self.templater.template(u.path or "/") if u.netloc == self.host else u.netloc
            fixed = target if target and ":p" not in target else None
            groups[(NTH.sub("", r["css_path"]), r["name_attr"], r["placeholder"], r["tag"], fixed)].append(r)
        out: list[Element] = []
        for (generic_path, *_), items in groups.items():
            first = items[0]
            href_tpl = None
            if first["href"] is not None and not first["href"].startswith(("mailto:", "javascript:", "#")):
                target = urlparse(urljoin(self.app_url + "/", first["href"]))
                if target.netloc == self.host:
                    href_tpl = self.templater.template(target.path or "/")
            labels = {_norm(i["label"]) for i in items}
            data_driven = len(items) >= 3 or (href_tpl is not None and ":p" in href_tpl and len(labels) > 1)
            label = _norm(first["label"])
            if data_driven:
                label = f"{len(items)} items, e.g. '{label[:40]}'"
            sig = "|".join([screen_id, first["tag"], first["name_attr"] or "", first["placeholder"] or "",
                            generic_path if data_driven else label, href_tpl or ""])
            out.append(Element(
                id="el_" + _hash(sig), screen_id=screen_id, tag=first["tag"], role=first["role"], label=label,
                name_attr=first["name_attr"], placeholder=first["placeholder"], input_type=first["input_type"],
                href_template=href_tpl, classes=first["classes"], css_path=first["css_path"], repeated=data_driven,
            ))
        return out

    async def _a11y_lines(self, page: Page) -> list[str]:
        """Playwright's accessibility snapshot, normalised for state identity: roles and names, with volatile
        values (numbers, dates, user content in lists) stripped and repeated list items collapsed."""
        try:
            snap = await page.locator("body").aria_snapshot(timeout=5000)
        except Exception:  # noqa: BLE001
            return []
        lines, prev = [], None
        for line in snap.splitlines():
            line = line.strip()
            if line.startswith("- /url:"):
                continue                                   # where a link goes is already in the element list
            line = re.sub(r"\d+", "#", line)
            m = re.match(r'^- (\w+)(?: "(.*?)")?(.*)$', line)
            if m:
                role, name, rest = m.groups()
                # keep the names of controls (Home, Sign in, Publish Article); drop user content (titles, text, dates)
                keep = role in ("link", "button", "textbox", "checkbox", "combobox", "tab") and name and len(name) <= 20
                line = f"- {role}" + (f' "{name}"' if keep else "") + rest.split(":")[0]
            if line and line != prev:
                lines.append(line)
            prev = line
        return lines

    async def _dry_run_clicks(self, page: Page, url: str, screen_id: str, elements: list[Element]) -> None:
        """Click each button with writes blocked; record the API calls it attempts and where it navigates."""
        clickable = [e for e in elements if e.tag == "button" or e.role == "button"
                     or (e.tag == "a" and e.href_template is None)][:15]
        for el in clickable:
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await self._fill_forms(page)
                self.calls.clear()
                await page.locator(el.css_path).first.click(timeout=3000)
                await page.wait_for_timeout(1200)
            except Exception:  # noqa: BLE001 - element gone, disabled or covered: skip it
                continue
            el.triggers = _dedupe([c for c in self.calls if c.blocked or c.method == "GET"])
            if urlparse(page.url).netloc != self.host:
                continue  # an external link: not part of this app
            landed = self.templater.template(urlparse(page.url).path or "/")
            if landed != screen_id.split("|")[0]:
                self.transitions.append(Transition(from_screen=screen_id, element_id=el.id, to_template=landed))
            self.elements[el.id] = el

    async def _fill_forms(self, page: Page) -> None:
        """Deterministic test data, so a dry-run submit exercises the real validation path."""
        values = {"email": "br-dryrun@example.com", "password": "dry-run-only", "username": "br-dryrun",
                  "title": "Dry run title", "description": "Dry run", "body": "Dry run body",
                  "bio": "Dry run", "image": "https://example.com/a.png"}
        for name, value in values.items():
            loc = page.locator(f'[name="{name}"]')
            if await loc.count():
                try:
                    await loc.first.fill(value, timeout=1000)
                except Exception:  # noqa: BLE001
                    pass

    def _retemplate(self) -> None:
        """Templates learned late can generalise ones decided early (e.g. a second username seen)."""
        rename: dict[str, str] = {}
        merged: dict[str, Screen] = {}
        for s in self.screens.values():
            s.template = self.templater.template(urlparse(s.sample_urls[0]).path or "/")
            new_id = f"{s.template}|{s.auth}"
            rename[s.id] = new_id
            if new_id in merged:
                merged[new_id].sample_urls += s.sample_urls
            else:
                s.id = new_id
                merged[new_id] = s
        self.screens = merged
        for e in self.elements.values():
            e.screen_id = rename.get(e.screen_id, e.screen_id)
            if e.href_template and ":p" not in e.href_template:
                e.href_template = self.templater.template(e.href_template)
        for t in self.transitions:
            t.from_screen = rename.get(t.from_screen, t.from_screen)
            if ":p" not in t.to_template:
                t.to_template = self.templater.template(t.to_template)
        visited = {s.template for s in self.screens.values()}
        self.unreached = [u for u in self.unreached
                          if not (u["reason"].startswith("linked") and self.templater.template(u["template"]) in visited)]


def _dedupe(calls: list[ApiCall]) -> list[ApiCall]:
    seen, out = set(), []
    for c in calls:
        k = (c.method, c.path)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def crawl(out_path=None) -> Crawl:
    result = asyncio.run(Crawler().run())
    out_path = out_path or SETTINGS.artifacts / "crawl.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.model_dump_json(indent=2))
    return result
