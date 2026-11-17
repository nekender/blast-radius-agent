"""Typed artifacts passed between stages. Every LLM output is parsed into one of these."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AuthState = Literal["anon", "authed", "owner"]


# ---------- UI layer (from the crawl) ----------

class ApiCall(BaseModel):
    method: str
    path: str                      # templated, e.g. /api/articles/:p
    blocked: bool = False          # True when the crawler stopped a write in dry-run mode


class Element(BaseModel):
    id: str                        # stable: hash(screen template, auth, signature)
    screen_id: str
    tag: str
    role: str | None = None
    label: str = ""                # accessible name / visible text, whitespace-normalised
    name_attr: str | None = None
    placeholder: str | None = None
    input_type: str | None = None
    href_template: str | None = None
    classes: list[str] = Field(default_factory=list)
    css_path: str = ""
    repeated: bool = False         # one of many data-driven items (article cards, tag pills)
    triggers: list[ApiCall] = Field(default_factory=list)   # observed by a dry-run click


class Screen(BaseModel):
    id: str                        # template + auth state, e.g. "/editor|authed"
    template: str                  # /article/:p
    auth: AuthState
    sample_urls: list[str]
    title: str = ""
    structure_hash: str = ""
    on_load_calls: list[ApiCall] = Field(default_factory=list)
    screenshot: str | None = None
    dom: str | None = None


class Transition(BaseModel):
    from_screen: str
    element_id: str
    to_template: str


class Crawl(BaseModel):
    app_url: str
    screens: list[Screen]
    elements: list[Element]
    transitions: list[Transition]
    unreached: list[dict] = Field(default_factory=list)   # templates seen in links but never visited, with reason
    journeys: list[dict] = Field(default_factory=list)    # paths the exploration agent took (explore.py)


# ---------- Requirements layer (from the spec) ----------

class Evidence(BaseModel):
    """What the UI must contain for the requirement to count as present."""
    texts: list[str] = Field(default_factory=list)
    name_attrs: list[str] = Field(default_factory=list)
    placeholders: list[str] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)
    api: list[str] = Field(default_factory=list)          # "PUT /api/articles/:p"


class Requirement(BaseModel):
    id: str
    statement: str
    source: str                    # file#section
    pages: list[str]               # route templates where it should appear
    needs: AuthState = "anon"      # minimum state in which it should be visible
    kind: Literal["element", "behaviour", "route"] = "element"
    evidence: Evidence = Field(default_factory=Evidence)


class SpecRequirements(BaseModel):
    """LLM output for one spec section."""
    requirements: list[Requirement]


# ---------- Code layer ----------

class TemplateElement(BaseModel):
    id: str                        # file:line
    component: str
    file: str
    line: int
    tag: str
    text: str = ""
    name_attr: str | None = None
    placeholder: str | None = None
    classes: list[str] = Field(default_factory=list)
    router_link: str | None = None
    handlers: list[str] = Field(default_factory=list)      # methods bound via (event)="..."
    identifiers: list[str] = Field(default_factory=list)   # fields/signals read in bindings
    directives: list[str] = Field(default_factory=list)    # attribute directives present (lowercased)
    child_selector: str | None = None                      # <app-foo> renders component app-foo
    parent: str | None = None                              # enclosing template element id


class Method(BaseModel):
    name: str
    start: int
    end: int
    calls_self: list[str] = Field(default_factory=list)       # this.foo()
    calls_service: list[str] = Field(default_factory=list)    # "ArticlesService.update"
    reads: list[str] = Field(default_factory=list)            # this.x identifiers


class Component(BaseModel):
    name: str                      # class name
    file: str
    kind: Literal["component", "directive", "service", "other"]
    selector: str | None = None
    template_file: str | None = None
    methods: list[Method] = Field(default_factory=list)
    lifecycle: list[str] = Field(default_factory=list)       # ngOnInit etc.
    endpoints: dict[str, str] = Field(default_factory=dict)  # service method -> "PUT /api/articles/:p"
    injects: dict[str, str] = Field(default_factory=dict)    # field name -> service class


class Route(BaseModel):
    template: str
    component: str | None
    file: str
    guarded: bool = False


class CodeMap(BaseModel):
    commit: str
    routes: list[Route]
    components: list[Component]
    template_elements: list[TemplateElement]
    layout_components: list[str]   # rendered on every screen (from app.component.html)


# ---------- Links ----------

class Link(BaseModel):
    src: str
    dst: str
    kind: str
    confidence: float
    method: str                    # how it was decided: "name_attr", "text", "api", "llm", ...
    evidence: str = ""


class Gap(BaseModel):
    requirement_id: str
    status: Literal["absent", "unknown", "partial"]
    searched_screens: list[str]
    reason: str


class Flow(BaseModel):
    id: str
    name: str
    goal: str = ""
    steps: list[str]               # screen ids, element ids and template element ids, in order
    narrative: list[str] = Field(default_factory=list)   # the same steps in words: "click 'New Article'"
    action_element: str
    calls: list[str] = Field(default_factory=list)       # API calls the final action makes (or can make, from code)
    requires_login: bool = False
    observed: bool = True          # False: stitched from code because the crawl never reached its last screen


class FlowName(BaseModel):
    id: str
    name: str                      # 3-8 words, imperative, product language
    goal: str                      # one sentence: what the user achieves


class FlowNames(BaseModel):
    flows: list[FlowName]


# ---------- Blast radius ----------

class Impact(BaseModel):
    kind: Literal["element", "screen", "flow", "requirement", "unmapped_code"]
    id: str
    label: str
    confidence: float
    why: list[str]                 # evidence chain, human-readable
    observed: bool = True


class BlastRadius(BaseModel):
    pr: int
    title: str
    url: str
    changed: list[dict]            # {file, unit, lines}
    impacts: list[Impact]
    coverage_lost: list[dict]
    needs_review: list[dict]
    assessment: dict | None = None   # the assessment agent's behaviour summary, adjustments and trace
