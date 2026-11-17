"""The assessment agent's guardrails, with the model replaced by a scripted answer."""
from blast_radius import assess as A
from blast_radius import llm
from blast_radius.models import BlastRadius, CodeMap, Impact


def _br():
    imp = lambda i, c, obs=True: Impact(kind="flow", id=i, label=i, confidence=c, why=["traversal"], observed=obs)
    return BlastRadius(pr=1, title="t", url="u", changed=[], impacts=[imp("a", 1.0), imp("b", 1.0), imp("c", 0.5, False)],
                       coverage_lost=[], needs_review=[])


def test_assessment_is_grounded_bounded_and_never_removes(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "available", lambda: True)
    result = {"behaviour_change": "x", "retest_first": ["b"], "adjustments": [
        {"id": "a", "confidence": 0.1, "reason": "setup step only"},       # inspected: capped at -0.5, floor 0.3
        {"id": "b", "confidence": 0.2, "reason": "never looked at it"},     # not inspected: rejected
        {"id": "c", "confidence": 0.95, "reason": "unobserved"},            # unobserved above 0.9: rejected
        {"id": "zzz", "confidence": 0.9, "reason": "invented"},             # not an impact: rejected
    ]}
    trace = [{"turn": 0, "tool": "inspect", "input": {"item_ids": ["a", "c"]}, "output": ""}]
    monkeypatch.setattr(llm, "agent_loop", lambda *a, **k: (result, trace))
    br = A.assess(_br(), {"files": []}, CodeMap(commit="x", routes=[], components=[], template_elements=[],
                                                 layout_components=[]), graph=None, use_cache=False)
    conf = {i.id: i.confidence for i in br.impacts}
    assert conf == {"a": 0.5, "b": 1.0, "c": 0.5}                 # nothing removed; only 'a' moved, capped
    assert br.assessment["applied"][0]["capped"] is True
    reasons = {r["id"]: r["rejected_because"] for r in br.assessment["rejected"]}
    assert "inspecting" in reasons["b"] and "0.9" in reasons["c"] and reasons["zzz"] == "not an impact id"
    assert br.impacts[0].id == "b"                                   # re-test order applied
