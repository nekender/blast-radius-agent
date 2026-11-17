"""Stage 1b · Explore to close gaps (LLM agent, guarded).

The breadth-first crawl can't reach states that need data you own: the edit page, the author's Delete
button. Those requirements end up 'unknown'. This agent reads the gaps, decides how to reach them, and
drives a real browser through tools until the graph can observe them.

  model decides   which gap to close, what to click, what to type (observe / click / fill / goto / finish)
  code enforces   the write policy: only creating an article titled 'br-test: ...' and deleting the
                  articles it created; every other write is blocked and reported back to the model as blocked
  code records    any new page reached as the content's owner (elements, screenshot, DOM, dry-run clicks)
  code cleans up  every article it created is deleted through the API in a `finally`, and leftovers from a
                  crashed run are deleted at the start; deletion is checked, not assumed

Writes to the live demo app are limited to that one test article, with Nekender's approval (17 Nov 2026).
"""
from __future__ import annotations

import asyncio
import json
import secrets
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright

from . import llm
from .config import SETTINGS
from .crawl import EXTRACT_JS, Crawler, template_api
from .models import ApiCall, Crawl, Screen, Transition

API = "https://api.realworld.show/api"
TEST_PREFIX = "br-test:"
ALLOWED = {("POST", "/api/articles"), ("DELETE", "/api/articles/:p")}

SYSTEM = """You are a QA exploration agent on a live web app. A crawler already mapped the pages anyone can reach;
some product requirements could not be checked because they only show up in states the crawler never created
(for example, controls only an article's author sees). Your job: reach those states so they can be observed.

You act only through tools. After each action, call observe to see where you are.
Rules:
- You are logged in as a test user. The only data you may create is ONE article whose title starts with
  "br-test:" and whose body says it is an automated test. Any other change (comments, follows, favourites,
  settings, other users' content) is blocked by the system and you will be told so.
- Do not try to delete the article; cleanup is handled for you.
- Prefer reaching each target page once. When the targets are reached or cannot be reached, call finish with a
  short summary of what you reached and what you could not."""


class Explorer(Crawler):
    def __init__(self, base: Crawl):
        super().__init__()
        for s in base.screens:
            self.screens[s.id] = s
            for u in s.sample_urls:
                self.templater.observe(urlparse(u).path)
        for e in base.elements:
            self.elements[e.id] = e
        self.transitions = list(base.transitions)
        self.unreached = list(base.unreached)
        self.base = base
        self.created: list[str] = []          # slugs of articles this run created
        self.blocked: list[str] = []
        self.allow_writes = True
        self.journey: list[dict] = []
        self.username = ""
        self.token = ""

    # ----- write policy, enforced in code -----

    async def _intercept(self, route) -> None:
        req = route.request
        call = ApiCall(method=req.method, path=template_api(req.url), blocked=req.method not in ("GET", "OPTIONS"))
        allowed = False
        if call.blocked and self.allow_writes and (call.method, call.path) in ALLOWED:
            if call.method == "POST":
                body = json.loads(req.post_data or "{}").get("article", {})
                allowed = str(body.get("title", "")).startswith(TEST_PREFIX) and not self.created
            else:
                allowed = any(req.url.rstrip("/").endswith(f"/articles/{s}") for s in self.created)
        if allowed:
            call.blocked = False
            self.calls.append(call)
            resp = await route.fetch()
            if call.method == "POST" and resp.ok:
                self.created.append((await resp.json())["article"]["slug"])
            await route.fulfill(response=resp)
            return
        self.calls.append(call)
        if call.blocked:
            self.blocked.append(f"{call.method} {call.path}")
            await route.abort()
        else:
            await route.continue_()

    # ----- cleanup, through the API, verified -----

    def _api(self) -> httpx.Client:
        return httpx.Client(base_url=API, headers={"Authorization": f"Token {self.token}"}, timeout=20)

    def cleanup(self, slugs: list[str] | None = None) -> list[dict]:
        """Delete the given slugs, or every leftover test article by this account. Returns what happened."""
        out = []
        with self._api() as c:
            if slugs is None:
                arts = c.get("/articles", params={"author": self.username, "limit": 50}).json().get("articles", [])
                slugs = [a["slug"] for a in arts if a["title"].startswith(TEST_PREFIX)]
            for s in slugs:
                r = c.delete(f"/articles/{s}")
                gone = c.get(f"/articles/{s}").status_code == 404
                out.append({"slug": s, "delete_status": r.status_code, "verified_gone": gone})
        return out

    # ----- recording an owner-state page (separate tab, all writes blocked) -----

    async def record_owner_page(self, ctx, url: str) -> str | None:
        path = urlparse(url).path
        self.templater.observe(path)
        tpl = self.templater.template(path)
        sid = f"{tpl}|owner"
        if sid in self.screens:
            return sid
        page = await ctx.new_page()
        self.allow_writes = False
        try:
            links = await self._visit(page, url, tpl, "owner")
            del links
        finally:
            self.allow_writes = True
            await page.close()
        return sid if sid in self.screens else None

    def _is_owner_url(self, url: str) -> bool:
        p = urlparse(url).path
        return any(s in p for s in self.created) or (self.username and p.rstrip("/").endswith(f"/profile/{self.username}"))

    # ----- the agent -----

    async def run_agent(self, goals: list[str], max_turns: int = 25) -> dict:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            self.token = await self.ensure_account(browser)
            self.username = json.loads((SETTINGS.state_dir / "account.json").read_text())["username"]
            leftovers = self.cleanup()
            ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
            await ctx.add_init_script(f"if (!localStorage.getItem('jwtToken')) localStorage.setItem('jwtToken', {json.dumps(self.token)});")
            await ctx.route("**/api/**", self._intercept)
            page = await ctx.new_page()
            await page.goto(f"{self.app_url}/", wait_until="networkidle")
            state = {"elements": []}
            loop = asyncio.get_running_loop()
            recorded: list[str] = []

            async def observe() -> str:
                await page.wait_for_load_state("networkidle")
                raw = [r for r in await page.evaluate(EXTRACT_JS) if r["tag"] in ("a", "button", "input", "textarea")]
                state["elements"] = raw[:60]
                if self._is_owner_url(page.url):
                    sid = await self.record_owner_page(ctx, page.url)
                    if sid and sid not in recorded:
                        recorded.append(sid)
                auth = await page.evaluate("window.__conduit_debug__ ? window.__conduit_debug__.getAuthState() : 'unknown'")
                lines = [f"{i}: <{r['tag']}> {r['label'] or r['placeholder'] or r['name_attr'] or ''}"
                         + (f" -> {r['href']}" if r["href"] else "") for i, r in enumerate(state["elements"])]
                blocked = f"\nwrites blocked so far: {sorted(set(self.blocked))}" if self.blocked else ""
                return (f"url: {page.url}\npage: {self.templater.template(urlparse(page.url).path or '/')}\n"
                        f"login state: {auth}\nyour test articles: {self.created or 'none yet'}{blocked}\n"
                        f"elements:\n" + "\n".join(lines))

            async def click(index: int) -> str:
                r = state["elements"][index]
                before = len(self.blocked)
                self.journey.append({"action": "click", "label": r["label"], "url": page.url, "css": r["css_path"]})
                await page.locator(r["css_path"]).first.click(timeout=5000)
                await page.wait_for_timeout(1500)
                new = self.blocked[before:]
                return f"clicked {r['label']!r}" + (f"; BLOCKED by policy: {new}" if new else "") + "\n" + await observe()

            async def fill(index: int, text: str) -> str:
                r = state["elements"][index]
                self.journey.append({"action": "fill", "label": r["label"] or r["placeholder"], "url": page.url, "css": r["css_path"]})
                await page.locator(r["css_path"]).first.fill(text, timeout=5000)
                return f"filled {r['placeholder'] or r['name_attr'] or index}"

            async def goto(path: str) -> str:
                if not path.startswith("/"):
                    return "error: give a path on this site, starting with /"
                self.journey.append({"action": "goto", "label": path, "url": page.url, "css": ""})
                await page.goto(f"{self.app_url}{path}", wait_until="networkidle")
                return await observe()

            def sync(coro_fn):
                # The agent loop is synchronous; tools run on the browser's event loop.
                def run(**kw):
                    return asyncio.run_coroutine_threadsafe(coro_fn(**kw), loop).result(timeout=90)
                return run

            tools = [
                llm.Tool("observe", "See the current page: url, login state, numbered interactive elements.", {}, sync(observe)),
                llm.Tool("click", "Click element number `index` from the last observe.", {"index": {"type": "integer"}}, sync(click)),
                llm.Tool("fill", "Type `text` into input number `index` from the last observe.",
                         {"index": {"type": "integer"}, "text": {"type": "string"}}, sync(fill)),
                llm.Tool("goto", "Open a path on this site, e.g. /editor.", {"path": {"type": "string"}}, sync(goto)),
                llm.Tool("finish", "Stop and summarise what you reached and what you could not.",
                         {"summary": {"type": "string"}}, None),
            ]
            prompt = ("Targets: requirements nobody has observed yet, and why:\n- " + "\n- ".join(goals)
                      + "\n\nStart with observe.")
            try:
                result, trace = await asyncio.to_thread(llm.agent_loop, SYSTEM, prompt, tools, "finish",
                                                        max_turns=max_turns)
            finally:
                deleted = self.cleanup(list(self.created)) if self.created else []
                await browser.close()
        return {"goals": goals, "finish": result, "trace": trace, "recorded_owner_screens": recorded,
                "created": self.created, "deleted": deleted, "leftovers_cleaned": leftovers,
                "blocked_writes": sorted(set(self.blocked)), "journey": self.journey}


def gap_goals(limit: int = 8) -> list[str]:
    links = json.loads((SETTINGS.artifacts / "links.json").read_text())
    reqs = {r["id"]: r for r in json.loads((SETTINGS.artifacts / "requirements.json").read_text())}
    goals = []
    for g in links["gaps"]:
        r = reqs.get(g["requirement_id"])
        if g["status"] == "unknown" and r and r["needs"] == "owner":
            goals.append(f"{r['statement']} (pages {', '.join(r['pages'])}): {g['reason'][:140]}")
    return goals[:limit]


def explore(max_turns: int = 25) -> dict:
    path = SETTINGS.artifacts / "crawl.json"
    base = Crawl.model_validate_json(path.read_text())
    ex = Explorer(base)
    goals = gap_goals()
    if not goals:
        return {"goals": [], "note": "no owner-state gaps to close"}
    import time
    llm.reset_ledger()
    t0 = time.monotonic()
    with llm.stage("explore"):
        report = asyncio.run(ex.run_agent(goals, max_turns=max_turns))
    report["cost"] = {"llm": llm.summarize(), "seconds_total": round(time.monotonic() - t0, 1)}
    ex._retemplate()
    merged = Crawl(app_url=base.app_url, screens=list(ex.screens.values()), elements=list(ex.elements.values()),
                   transitions=ex.transitions, unreached=ex.unreached, journeys=[_journey(ex, report)])
    path.write_text(merged.model_dump_json(indent=2))
    (SETTINGS.artifacts / "explore_trace.json").write_text(json.dumps(report, indent=1, default=str))
    return report


def _journey(ex: Explorer, report: dict) -> dict:
    """The agent's own path, as ids where the recorded screens let us resolve them."""
    steps = []
    for j in report["journey"]:
        tpl = ex.templater.template(urlparse(j["url"]).path or "/")
        auth = "owner" if ex._is_owner_url(j["url"]) else "authed"
        sid = f"{tpl}|{auth}"
        el = next((e.id for e in ex.elements.values() if e.screen_id == sid and j["label"] and e.label == j["label"]), None)
        steps.append({"screen": sid, "element": el, "action": j["action"], "label": j["label"]})
    return {"steps": steps, "summary": (report.get("finish") or {}).get("summary", "")}
