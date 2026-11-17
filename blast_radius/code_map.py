"""Stage 3 · Map the code (deterministic).

Reads the Angular repo at a given commit (via `git show`, no checkout) and extracts:
- routes            -> which page component serves which URL template
- components        -> selector, template, methods (with line ranges), injected services
- services          -> method -> HTTP endpoint ("PUT /api/articles/:p")
- template elements -> each tag in each template, with event handlers, bindings, routerLinks,
                       name/placeholder/text, and the @if conditions it sits under

Scope decision: regex + a small HTML parser, framework-aware for Angular only. A full TypeScript
AST (tree-sitter or ts-morph) is the production answer; for one 63-file Angular app this gets
method-level precision at a fraction of the setup cost. See docs/design.md.
"""
from __future__ import annotations

import posixpath
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from .config import SETTINGS
from .models import CodeMap, Component, Method, Route, TemplateElement

KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "constructor", "super"}
METHOD_RE = re.compile(r"^\s{2}(?:@\w+\([^)]*\)\s*)?(?:(?:public|private|protected|async|static|get|set)\s+)*(\w+)\s*(?:<[^>]*>)?\s*\(.*\)\s*(?::\s*.+)?\{\s*$")
HANDLER_CALL = re.compile(r"(\w+)\s*\(")
IDENT = re.compile(r"\b([a-zA-Z_]\w*)\b")
JS_WORDS = {"true", "false", "null", "undefined", "async", "as", "of", "track", "let", "this", "event", "$event", "date", "json"}


# ---------------- git helpers ----------------

def git(*args: str, repo: Path = SETTINGS.repo_dir) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def read_at(commit: str, path: str) -> str | None:
    try:
        return git("show", f"{commit}:{path}")
    except subprocess.CalledProcessError:
        return None


def list_files(commit: str) -> list[str]:
    return [f for f in git("ls-tree", "-r", "--name-only", commit, "src/app").splitlines()]


# ---------------- brace helpers ----------------

def match_close(text: str, start: int, open_ch: str = "{", close_ch: str = "}") -> int:
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if text.startswith("//", i):          # line comment: an apostrophe in "don't" is not a string
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        if c in "'\"`":  # skip string literals
            j = i + 1
            while j < len(text) and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text) - 1


def top_level_objects(array_body: str) -> list[str]:
    objs, i = [], 0
    while i < len(array_body):
        if array_body[i] == "{":
            j = match_close(array_body, i)
            objs.append(array_body[i:j + 1])
            i = j + 1
        else:
            i += 1
    return objs


def own_key(obj: str, key: str) -> str | None:
    """Value text of `key:` at depth 1 of an object literal."""
    depth = 0
    for m in re.finditer(r"[{}\[\]()]|\b" + key + r"\s*:", obj):
        tok = m.group(0)
        if tok in "{[(":
            depth += 1
        elif tok in "}])":
            depth -= 1
        elif depth == 1:
            start = m.end()
            rest = obj[start:].lstrip()
            offset = start + (len(obj[start:]) - len(rest))
            if rest[:1] in "[{":
                end = match_close(obj, offset, rest[0], "]" if rest[0] == "[" else "}")
                return obj[offset:end + 1]
            end = offset
            depth2 = 0
            while end < len(obj) and not (obj[end] == "," and depth2 == 0) and not (obj[end] == "}" and depth2 == 0):
                depth2 += obj[end] in "([" and 1 or 0
                depth2 -= obj[end] in ")]" and 1 or 0
                end += 1
            return obj[offset:end].strip()
    return None


# ---------------- routes ----------------

def _resolve(from_file: str, spec: str) -> str:
    p = posixpath.normpath(posixpath.join(posixpath.dirname(from_file), spec))
    return p if p.endswith(".ts") else p + ".ts"


def parse_routes(commit: str, files: list[str], class_of: dict[str, str]) -> list[Route]:
    routes: list[Route] = []

    def walk(file: str, text: str, body: str, prefix: str, parent_guard: bool) -> None:
        imports = {m.group(1): m.group(2) for m in re.finditer(r"import\s*\{\s*(\w+)\s*\}\s*from\s*'([^']+)'", text)}
        for obj in top_level_objects(body):
            path = (own_key(obj, "path") or "''").strip("'\"")
            full = posixpath.join(prefix, path) if path else prefix
            guard = parent_guard or own_key(obj, "canActivate") is not None
            comp = None
            lc = own_key(obj, "loadComponent")
            if lc and (m := re.search(r"import\('([^']+)'\)", lc)):
                comp = class_of.get(_resolve(file, m.group(1)))
            elif (c := own_key(obj, "component")) and c in imports:
                comp = class_of.get(_resolve(file, imports[c]))
            if comp:
                routes.append(Route(template="/" + full.strip("/"), component=comp, file=file, guarded=guard))
            children = own_key(obj, "children")
            if children:
                walk(file, text, children[1:-1], full, guard)
            lch = own_key(obj, "loadChildren")
            if lch and (m := re.search(r"import\('([^']+)'\)", lch)):
                child_file = _resolve(file, m.group(1))
                child_text = read_at(commit, child_file) or ""
                if (arr := re.search(r"Routes\s*=\s*\[", child_text)):
                    start = arr.end() - 1
                    walk(child_file, child_text, child_text[start + 1:match_close(child_text, start, "[", "]")], full, guard)

    for f in files:
        if f.endswith("app.routes.ts"):
            text = read_at(commit, f) or ""
            if (arr := re.search(r"Routes\s*=\s*\[", text)):
                start = arr.end() - 1
                walk(f, text, text[start + 1:match_close(text, start, "[", "]")], "", False)
    for r in routes:
        r.template = re.sub(r":\w+", ":p", r.template)
    return routes


# ---------------- components / services ----------------

ARROW_FIELD = re.compile(r"^  (?:(?:public|private|protected|readonly|static)\s+)*(\w+)\s*=\s*(?:async\s*)?"
                         r"(?:\([^)]*\)|\w+)\s*(?::\s*[^=]+)?=>\s*\{", re.M)
METHOD_START = re.compile(r"^  (?:@\w+\([^)]*\)\s*)?(?:(?:public|private|protected|async|static|override|get|set)\s+)*"
                          r"(\w+)\s*(?:<[^>]*>)?\s*\(", re.M)


def _body_brace(text: str, i: int) -> int | None:
    """After a parameter list: skip an optional return type (which may contain '{' inside <...>, e.g.
    Observable<{ user: User }>) and return the index of the body's '{'. None if a ';' or '=' comes first."""
    angle = 0
    while i < len(text):
        if text.startswith("=>", i):
            i += 2
            continue
        c = text[i]
        if c == "<":
            angle += 1
        elif c == ">":
            angle -= 1
        elif angle == 0 and c == "{":
            return i
        elif angle == 0 and c in ";=":
            return None
        i += 1
    return None


def parse_methods(text: str) -> list[Method]:
    """Class methods, including signatures that span several lines:
         intercept(
           req: HttpRequest<any>,
         ): Observable<HttpEvent<any>> {"""
    methods: list[Method] = []
    for m in METHOD_START.finditer(text):
        name = m.group(1)
        if name in KEYWORDS:
            continue
        close = match_close(text, m.end() - 1, "(", ")")
        brace = _body_brace(text, close + 1)
        if brace is None:
            continue          # an overload, a call, or a field initialiser, not a method body
        methods.append(_method(name, text, m.start(), brace, match_close(text, brace)))
    for m in ARROW_FIELD.finditer(text):     # submit = () => { ... }: a method in all but syntax
        if m.group(1) in KEYWORDS or any(x.name == m.group(1) for x in methods):
            continue
        brace = m.end() - 1
        methods.append(_method(m.group(1), text, m.start(), brace, match_close(text, brace)))
    # constructor: treat like a lifecycle method
    if (m := re.search(r"^\s{2}constructor\s*\(", text, re.M)):
        open_idx = text.index("{", match_close(text, m.end() - 1, "(", ")"))
        methods.append(_method("constructor", text, m.start(), open_idx, match_close(text, open_idx)))
    return methods


def _method(name: str, text: str, start: int, brace: int, close_idx: int) -> Method:
    body = text[brace:close_idx]
    return Method(
        name=name, start=text.count("\n", 0, start) + 1, end=text.count("\n", 0, close_idx) + 1,
        calls_self=sorted(set(re.findall(r"this\.(\w+)\s*\(", body))),
        reads=sorted(set(re.findall(r"this\.(\w+)", body))),
        calls_service=sorted({f"{a}.{b}" for a, b in re.findall(r"this\.(\w+)\s*\.\s*(\w+)\s*\(", body)}),
    )


def _path_expr(expr: str) -> str:
    """'/profiles/' + username + '/follow'  or  `/articles/${slug}`  ->  /profiles/:p/follow"""
    out = []
    for part in re.split(r"\s*\+\s*", expr.strip()):
        if part[:1] in "'\"`":
            out.append(re.sub(r"\$\{[^}]+\}", ":p", part.strip("'\"`")))
        elif part.startswith("("):
            continue  # conditional suffix such as ('/feed' if ...): ignore, base path is enough
        else:
            out.append(":p")
    return "".join(out).replace("/:p:p", "/:p")


def parse_endpoints(text: str, methods: list[Method]) -> dict[str, str]:
    lines = text.splitlines()
    out = {}
    for m in methods:
        body = "\n".join(lines[m.start - 1:m.end])
        hit = re.search(r"\.(get|post|put|delete|patch)\s*(?:<[^(]*>)?\s*\(", body)
        if not hit:
            continue
        start = hit.end()
        depth, i = 0, start
        while i < len(body):
            c = body[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                break
            i += 1
        out[m.name] = f"{hit.group(1).upper()} /api{_path_expr(body[start:i])}"
    return out


def parse_injects(text: str) -> dict[str, str]:
    out = {}
    for field, cls in re.findall(r"(?:private|public|protected)\s+(?:readonly\s+)?(\w+)\s*:\s*(\w+)", text):
        out[field] = cls
    for field, cls in re.findall(r"(\w+)\s*=\s*inject\((\w+)\)", text):
        out[field] = cls
    return out


# ---------------- templates ----------------

class _TemplateParser(HTMLParser):
    VOID = {"img", "input", "br", "hr", "meta", "link"}

    def __init__(self, component: str, file: str, line_offset: int):
        super().__init__(convert_charrefs=True)
        # NB: HTMLParser uses self.offset for its own column bookkeeping, so the line offset needs another name
        self.component, self.file, self.line_offset = component, file, line_offset
        self.stack: list[TemplateElement] = []
        self.out: list[TemplateElement] = []
        self.conditions: list[str | None] = []
        self._last_if: str | None = None

    def handle_starttag(self, tag, attrs):
        line = self.getpos()[0] + self.line_offset
        a = {k: (v or "") for k, v in attrs}
        handlers, idents, directives = [], set(), []
        router_link = a.get("routerlink") or None
        for k, v in a.items():
            if k.startswith("(") and k.endswith(")"):
                handlers += [h for h in HANDLER_CALL.findall(v) if h not in JS_WORDS]
            if k.startswith(("[", "*")) or k.startswith("[("):
                idents |= {w for w in IDENT.findall(v) if w not in JS_WORDS}
            if k == "[routerlink]":
                parts = [p.strip() for p in v.strip("[]").split(",")]
                router_link = "/".join(p.strip("'\"") if p.startswith(("'", '"')) else ":p" for p in parts)
            bare = k.strip("[]()*")
            if bare and bare not in {"class", "type", "name", "placeholder", "href", "routerlink", "src", "alt", "rows", "title"}:
                directives.append(bare)
        el = TemplateElement(
            id=f"{self.file}:{line}:{tag}", component=self.component, file=self.file, line=line, tag=tag,
            name_attr=a.get("name") or a.get("formcontrolname") or None, placeholder=a.get("placeholder") or None,
            classes=a.get("class", "").split(), router_link=(router_link or None),
            handlers=sorted(set(handlers)), identifiers=sorted(idents), directives=directives,
            child_selector=tag if "-" in tag else None,
            parent=self.stack[-1].id if self.stack else None,
        )
        if any(self.conditions):
            el.identifiers = sorted(set(el.identifiers) | {f"@if:{c}" for c in self.conditions if c})
        self.out.append(el)
        if tag not in self.VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        # Angular control flow lives in text nodes: track @if conditions so elements know their visibility rule.
        # Every block opener (@if, @else, @for, @switch, @case, @defer, ...) pushes; its '}' pops. Interpolation
        # braces ({{ x }}) are removed first so they can't pop anything.
        flow = re.sub(r"\{\{.*?\}\}", " ", data, flags=re.S)
        for m in re.finditer(r"@(if|else if|else|for|switch|case|default|defer|empty|placeholder|loading)\b\s*"
                             r"(?:\((.*?)\))?\s*\{|\}", flow):
            tok = m.group(0)
            if tok == "}":
                if self.conditions:
                    self.conditions.pop()
            elif m.group(1) == "if":
                self.conditions.append(re.sub(r"\s+", " ", m.group(2) or "")[:60])
            elif m.group(1) in ("else", "else if"):
                # the closing '}' of the @if already popped it; record the negation (and the new test, if any)
                prev = self._last_if or ""
                cond = f"not {prev}" + (f" and {re.sub(chr(92) + 's+', ' ', m.group(2))[:40]}" if m.group(2) else "")
                self.conditions.append(cond[:60])
            else:
                self.conditions.append(None)      # a non-conditional block: still needs its '}' matched
            if m.group(1) == "if":
                self._last_if = self.conditions[-1]
        text = re.sub(r"\{\{.*?\}\}", " ", data, flags=re.S)
        text = re.sub(r"@(if|for|else|switch|case)\b[^{]*\{|\}", " ", text)
        text = re.sub(r"\s+", " ", text.replace(" ", " ")).strip()
        idents = {w for expr in re.findall(r"\{\{(.*?)\}\}", data, flags=re.S) for w in IDENT.findall(expr)} - JS_WORDS
        for_idents = {w for expr in re.findall(r"@for\s*\((.*?)\)", data) for w in IDENT.findall(expr)} - JS_WORDS
        if self.stack:
            target = self.stack[-1]
            if text:
                target.text = (target.text + " " + text).strip()[:80]
            target.identifiers = sorted(set(target.identifiers) | idents | for_idents)


def parse_template(html: str, component: str, file: str, line_offset: int = 0) -> list[TemplateElement]:
    p = _TemplateParser(component, file, line_offset)
    p.feed(html)
    return p.out


# ---------------- build ----------------

def build(commit: str = "HEAD") -> CodeMap:
    sha = git("rev-parse", commit).strip()
    files = list_files(sha)
    components: list[Component] = []
    elements: list[TemplateElement] = []
    class_of: dict[str, str] = {}

    for f in files:
        if not f.endswith(".ts") or f.endswith(".spec.ts"):
            continue
        text = read_at(sha, f) or ""
        cls = re.search(r"export\s+(?:default\s+)?class\s+(\w+)", text)
        if not cls:
            continue
        name = cls.group(1)
        class_of[f] = name
        kind = ("component" if "@Component(" in text else "directive" if "@Directive(" in text
                else "service" if "@Injectable(" in text else "other")
        selector = (re.search(r"selector:\s*'([^']+)'", text) or [None, None])[1]
        methods = parse_methods(text)
        comp = Component(name=name, file=f, kind=kind, selector=selector, methods=methods,
                         lifecycle=[m.name for m in methods if m.name.startswith("ng") or m.name == "constructor"],
                         injects=parse_injects(text))
        if kind == "service":
            comp.endpoints = parse_endpoints(text, methods)
        if kind == "component":
            if (t := re.search(r"templateUrl:\s*'([^']+)'", text)):
                tf = _resolve(f, t.group(1)).removesuffix(".ts")
                comp.template_file = tf
                elements += parse_template(read_at(sha, tf) or "", name, tf)
            elif (t := re.search(r"template:\s*`", text)):
                end = text.index("`", t.end())
                comp.template_file = f
                elements += parse_template(text[t.end():end], name, f, line_offset=text.count("\n", 0, t.end()))
        components.append(comp)

    routes = parse_routes(sha, files, class_of)
    root = next((c for c in components if c.name == "AppComponent"), None)
    layout = []
    if root:
        sel = {c.selector: c.name for c in components if c.selector}
        layout = [sel[e.child_selector] for e in elements if e.component == root.name and e.child_selector in sel]
    return CodeMap(commit=sha, routes=routes, components=components, template_elements=elements, layout_components=layout)


def save(cm: CodeMap, name: str = "codemap.json") -> Path:
    out = SETTINGS.artifacts / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(cm.model_dump_json(indent=2))
    return out
