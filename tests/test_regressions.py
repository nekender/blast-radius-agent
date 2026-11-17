"""Regression tests for bugs found in review (17 Nov 2026)."""
from blast_radius import assess as A
from blast_radius import llm
from blast_radius.blast import changed_lines, units_for
from blast_radius.code_map import parse_methods, parse_template
from blast_radius.models import BlastRadius, CodeMap, Impact


def test_template_line_numbers_are_real_lines():
    # HTMLParser keeps its own self.offset; a name clash made every line wrong (line + column)
    els = parse_template("<div>\n  <p>x</p>\n        <button (click)=\"go()\">Go</button>\n</div>", "C", "c.html")
    assert {e.tag: e.line for e in els}["button"] == 3


def test_changed_line_inside_a_multiline_tag_maps_to_that_element():
    els = parse_template("<div>\n<button\n  (click)=\"go()\">\n  Publish\n</button>\n</div>", "C", "c.html")
    cm = CodeMap(commit="x", routes=[], components=[], template_elements=els, layout_components=[])
    assert [u["tag"] for u in units_for(cm, "c.html", {4})] == ["button"]


def test_apostrophe_in_comment_does_not_swallow_the_next_method():
    ts = "class A {\n  foo() {\n    // don't\n    return 1;\n  }\n\n  bar() {\n    return 2;\n  }\n}\n"
    assert {m.name: (m.start, m.end) for m in parse_methods(ts)} == {"foo": (2, 5), "bar": (7, 9)}


def test_interpolation_braces_do_not_end_an_if_block():
    els = parse_template("@if (authed) {\n<span>{{ user.name }}</span>\n<button>Go</button>\n}", "C", "c.html")
    btn = next(e for e in els if e.tag == "button")
    assert "@if:authed" in btn.identifiers


def test_no_newline_marker_does_not_shift_lines():
    assert changed_lines("@@ -1,3 +1,3 @@\n a\n-b\n\\ No newline at end of file\n+c\n d") == ({2}, {2})


def test_floor_never_lifts_and_inspect_cap_counts(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    import dataclasses
    monkeypatch.setattr(A, "SETTINGS", dataclasses.replace(A.SETTINGS, cache_dir=__import__("pathlib").Path(
        __import__("tempfile").mkdtemp())))   # never write into the project's .cache
    br = BlastRadius(pr=1, title="t", url="u", changed=[], coverage_lost=[], needs_review=[],
                     impacts=[Impact(kind="flow", id=f"f{i}", label=f"f{i}", confidence=0.2 if i == 0 else 1.0, why=["x"])
                              for i in range(45)])
    ids = [f"f{i}" for i in range(45)]
    result = {"behaviour_change": "x", "retest_first": [], "adjustments": [
        {"id": "f0", "confidence": 0.1, "reason": "lower"}, {"id": "f44", "confidence": 0.5, "reason": "beyond cap"}]}
    trace = [{"turn": 0, "tool": "inspect", "input": {"item_ids": ids}, "output": ""}]
    monkeypatch.setattr(llm, "agent_loop", lambda *a, **k: (result, trace))
    br = A.assess(br, {"files": []}, CodeMap(commit="x", routes=[], components=[], template_elements=[],
                                              layout_components=[]), graph=None, use_cache=False)
    conf = {i.id: i.confidence for i in br.impacts}
    assert conf["f0"] == 0.2          # already below the 0.3 floor: neither lifted to it nor pushed further down
    assert conf["f44"] == 1.0         # only the first 40 ids of one inspect call count as inspected
