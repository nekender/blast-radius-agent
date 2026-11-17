from blast_radius.blast import changed_lines
from blast_radius.code_map import _path_expr, parse_methods, parse_template
from blast_radius.crawl import Templater, template_api


def test_templater_learns_params_from_many_values():
    t = Templater()
    for p in ["/article/a-b", "/article/c-d", "/profile/ann", "/profile/bob", "/profile/ann/favorites"]:
        t.observe(p)
    assert t.template("/article/x-y") == "/article/:p"
    assert t.template("/profile/zed") == "/profile/:p"
    assert t.template("/profile/ann/favorites") == "/profile/:p/favorites"
    assert t.template("/login") == "/login"


def test_api_template():
    assert template_api("https://x/api/articles/my-slug/comments/12") == "/api/articles/:p/comments/:p"


def test_path_expr():
    assert _path_expr("'/profiles/' + username + '/follow'") == "/profiles/:p/follow"
    assert _path_expr("`/articles/${slug}`") == "/articles/:p"


def test_changed_lines():
    patch = "@@ -10,3 +10,4 @@\n a\n-b\n+c\n+d\n e"
    new, old = changed_lines(patch)
    assert new == {11, 12} and old == {11}


def test_methods_and_template_bindings():
    ts = "class A {\n  save(): void {\n    this.helper();\n    this.svc.update(1);\n  }\n\n  helper() {\n  }\n}\n"
    ms = {m.name: m for m in parse_methods(ts)}
    assert ms["save"].start == 2 and ms["save"].end == 5
    assert ms["save"].calls_self == ["helper"] and "svc.update" in ms["save"].calls_service
    els = parse_template('@if (isAuthor) {\n<button (click)="save()">Save</button>\n}', "A", "a.html")
    btn = next(e for e in els if e.tag == "button")
    assert btn.handlers == ["save"] and btn.text == "Save" and "@if:isAuthor" in btn.identifiers
