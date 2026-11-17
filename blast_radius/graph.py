"""Neo4j: write the three-layer graph, and the queries the blast radius needs.

Schema (justified in docs/design.md against the blast-radius query):

  Requirements   (:Requirement)-[:COVERED_BY {confidence, method, evidence}]->(:Element|:Screen)
                 (:Requirement)-[:HAS_GAP]->(:Gap {status: absent|unknown, reason})-[:SEARCHED]->(:Screen)
                 (:Requirement)-[:EXPECTS_API]->(:Endpoint)
  UI             (:Screen)-[:HAS]->(:Element)-[:NAVIGATES_TO]->(:Screen)
                 (:Element)-[:CALLS {observed: true}]->(:Endpoint)        dry-run click
                 (:Flow)-[:STEP {order}]->(:Screen|:Element|:TemplateElement)
  Code           (:File)-[:DEFINES]->(:Component)-[:HAS_METHOD]->(:Method)
                 (:Component)-[:HAS_TEMPLATE]->(:TemplateElement)
                 (:Method)-[:AFFECTS {why}]->(:TemplateElement)
                 (:Method)-[:CALLS_API]->(:Endpoint)
  Code <-> UI    (:TemplateElement)-[:RENDERS {confidence, method, evidence}]->(:Element)
                 (:Screen)-[:SERVED_BY]->(:Component)

Absence is a node, not a missing edge: a Gap records that we looked (which screens, in which state) and
why we think it's absent or unknown. "No COVERED_BY edge" alone can't tell "absent" from "never looked".
"""
from __future__ import annotations

from neo4j import GraphDatabase

from .config import SETTINGS
from .models import CodeMap, Crawl, Flow, Gap, Link, Requirement

CONSTRAINTS = ["Requirement", "Screen", "Element", "Flow", "Gap", "File", "Component", "Method",
               "TemplateElement", "Endpoint"]


class Graph:
    def __init__(self):
        self.driver = GraphDatabase.driver(SETTINGS.neo4j_uri, auth=(SETTINGS.neo4j_user, SETTINGS.neo4j_password))

    def close(self):
        self.driver.close()

    def run(self, cypher: str, **params):
        records, _, _ = self.driver.execute_query(cypher, **params)
        return [r.data() for r in records]

    def reset(self):
        self.run("MATCH (n) DETACH DELETE n")
        for label in CONSTRAINTS:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE")

    def nodes(self, label: str, rows: list[dict]):
        if rows:
            self.run(f"UNWIND $rows AS r MERGE (n:{label} {{id: r.id}}) SET n += r", rows=rows)

    def edges(self, a: str, rel: str, b: str, rows: list[dict]):
        """rows: {src, dst, props}"""
        if rows:
            self.run(f"UNWIND $rows AS r MATCH (x:{a} {{id: r.src}}) MATCH (y:{b} {{id: r.dst}}) "
                     f"MERGE (x)-[e:{rel}]->(y) SET e += r.props", rows=rows)

    # ------------- load everything -------------

    def load(self, crawl: Crawl, cm: CodeMap, reqs: list[Requirement], code_ui: list[Link], method_links: list[Link],
             req_links: list[Link], gaps: list[Gap], flows: list[Flow]):
        self.reset()
        # UI
        self.nodes("Screen", [{"id": s.id, "template": s.template, "auth": s.auth, "url": s.sample_urls[0],
                               "title": s.title, "screenshot": s.screenshot} for s in crawl.screens])
        self.nodes("Element", [{"id": e.id, "label": e.label, "tag": e.tag, "name_attr": e.name_attr,
                                "placeholder": e.placeholder, "repeated": e.repeated, "screen": e.screen_id}
                               for e in crawl.elements])
        self.edges("Screen", "HAS", "Element", [{"src": e.screen_id, "dst": e.id, "props": {}} for e in crawl.elements])
        auth_of = {e.id: e.screen_id.split("|")[1] for e in crawl.elements}
        screen_ids = {s.id for s in crawl.screens}
        self.edges("Element", "NAVIGATES_TO", "Screen", [
            {"src": t.element_id, "dst": f"{t.to_template}|{auth_of[t.element_id]}", "props": {}}
            for t in crawl.transitions if t.element_id in auth_of and f"{t.to_template}|{auth_of[t.element_id]}" in screen_ids])
        endpoints = {f"{c.method} {c.path}" for e in crawl.elements for c in e.triggers}
        endpoints |= {ep for c in cm.components for ep in c.endpoints.values()}
        endpoints |= {ep for r in reqs for ep in r.evidence.api}
        self.nodes("Endpoint", [{"id": ep} for ep in sorted(endpoints)])
        self.edges("Element", "CALLS", "Endpoint", [{"src": e.id, "dst": f"{c.method} {c.path}", "props": {"observed": True}}
                                                   for e in crawl.elements for c in e.triggers])
        self.edges("Screen", "LOADS", "Endpoint", [{"src": s.id, "dst": f"{c.method} {c.path}", "props": {}}
                                                  for s in crawl.screens for c in s.on_load_calls])
        # Code
        self.nodes("File", [{"id": f} for f in sorted({c.file for c in cm.components} | {t.file for t in cm.template_elements})])
        self.nodes("Component", [{"id": c.name, "kind": c.kind, "selector": c.selector, "file": c.file} for c in cm.components])
        self.edges("File", "DEFINES", "Component", [{"src": c.file, "dst": c.name, "props": {}} for c in cm.components])
        self.nodes("Method", [{"id": f"{c.name}.{m.name}", "name": m.name, "start": m.start, "end": m.end, "file": c.file}
                              for c in cm.components for m in c.methods])
        self.edges("Component", "HAS_METHOD", "Method", [{"src": c.name, "dst": f"{c.name}.{m.name}", "props": {}}
                                                        for c in cm.components for m in c.methods])
        self.nodes("TemplateElement", [{"id": t.id, "tag": t.tag, "text": t.text, "line": t.line, "file": t.file,
                                        "component": t.component, "conditions": [i[4:] for i in t.identifiers if i.startswith("@if:")]}
                                       for t in cm.template_elements])
        self.edges("Component", "HAS_TEMPLATE", "TemplateElement", [{"src": t.component, "dst": t.id, "props": {}}
                                                                   for t in cm.template_elements])
        self.edges("Method", "AFFECTS", "TemplateElement", [{"src": l.src, "dst": l.dst, "props": {"why": l.evidence, "confidence": l.confidence}}
                                                           for l in method_links if l.kind == "AFFECTS"])
        self.edges("Method", "CALLS_API", "Endpoint", [{"src": l.src, "dst": l.dst, "props": {}}
                                                      for l in method_links if l.kind == "CALLS_API"])
        # Code <-> UI
        self.edges("TemplateElement", "RENDERS", "Element", [
            {"src": l.src, "dst": l.dst, "props": {"confidence": l.confidence, "method": l.method, "evidence": l.evidence}}
            for l in code_ui if l.kind == "RENDERS"])
        self.edges("Screen", "SERVED_BY", "Component", [{"src": l.src, "dst": l.dst, "props": {}}
                                                       for l in code_ui if l.kind == "SERVED_BY"])
        # Requirements
        self.nodes("Requirement", [{"id": r.id, "statement": r.statement, "source": r.source, "needs": r.needs,
                                    "kind": r.kind, "pages": r.pages} for r in reqs])
        self.edges("Requirement", "EXPECTS_API", "Endpoint", [{"src": r.id, "dst": ep, "props": {}}
                                                             for r in reqs for ep in r.evidence.api])
        for target in ("Element", "Screen"):
            self.edges("Requirement", "COVERED_BY", target, [
                {"src": l.src, "dst": l.dst, "props": {"confidence": l.confidence, "method": l.method, "evidence": l.evidence}}
                for l in req_links])
        self.nodes("Gap", [{"id": f"gap:{g.requirement_id}", "status": g.status, "reason": g.reason} for g in gaps])
        self.edges("Requirement", "HAS_GAP", "Gap", [{"src": g.requirement_id, "dst": f"gap:{g.requirement_id}", "props": {}} for g in gaps])
        self.edges("Gap", "SEARCHED", "Screen", [{"src": f"gap:{g.requirement_id}", "dst": s, "props": {}}
                                                for g in gaps for s in g.searched_screens])
        # Flows
        self.nodes("Flow", [{"id": f.id, "name": f.name, "observed": f.observed, "action_call": f.action_call} for f in flows])
        for target in ("Screen", "Element", "TemplateElement"):
            self.edges("Flow", "STEP", target, [{"src": f.id, "dst": s, "props": {"order": i}}
                                                for f in flows for i, s in enumerate(f.steps)])

    # ------------- the blast-radius queries -------------

    def impacted_ui(self, method_ids: list[str]) -> list[dict]:
        """Changed methods -> template elements -> rendered UI elements -> screens."""
        return self.run("""
            UNWIND $mids AS mid
            MATCH (m:Method {id: mid})-[a:AFFECTS]->(t:TemplateElement)
            OPTIONAL MATCH (t)-[r:RENDERS]->(e:Element)<-[:HAS]-(s:Screen)
            RETURN m.id AS method, t.id AS template_element, t.text AS t_text, t.tag AS t_tag, t.conditions AS conditions,
                   a.why AS why, a.confidence AS a_conf,
                   e.id AS element, e.label AS label, r.confidence AS r_conf, r.method AS r_method, r.evidence AS r_evidence,
                   s.id AS screen, s.template AS template, s.auth AS auth
        """, mids=method_ids)

    def endpoints_of(self, method_ids: list[str]) -> list[dict]:
        return self.run("""
            UNWIND $mids AS mid
            MATCH (m:Method {id: mid})-[:CALLS_API]->(ep:Endpoint)
            OPTIONAL MATCH (e)-[:CALLS|LOADS]->(ep) WHERE e:Element OR e:Screen
            RETURN m.id AS method, ep.id AS endpoint, collect(DISTINCT e.id) AS observed_by
        """, mids=method_ids)

    def flows_touching(self, ids: list[str]) -> list[dict]:
        return self.run("""
            MATCH (f:Flow)-[st:STEP]->(x) WHERE x.id IN $ids
            RETURN f.id AS flow, f.name AS name, f.observed AS observed, f.action_call AS call, collect(x.id) AS via
        """, ids=ids)

    def requirements_touching(self, element_ids: list[str], endpoints: list[str]) -> list[dict]:
        """Requirements proven by an at-risk element, or expecting an endpoint the change reaches.
        `only` = true when every piece of UI evidence for the requirement is at risk: its coverage is lost."""
        return self.run("""
            MATCH (q:Requirement)
            OPTIONAL MATCH (q)-[c:COVERED_BY]->(x) WHERE c.confidence >= $min_conf
            WITH q, collect(x.id) AS cov, collect(c.confidence) AS confs
            OPTIONAL MATCH (q)-[:EXPECTS_API]->(ep:Endpoint) WHERE ep.id IN $eps
            OPTIONAL MATCH (q)-[:HAS_GAP]->(g:Gap)
            WITH q, cov, confs, collect(ep.id) AS eps, g,
                 [i IN cov WHERE i IN $els] AS at_risk
            WHERE size(at_risk) > 0 OR size(eps) > 0
            RETURN q.id AS id, q.statement AS statement, q.needs AS needs, at_risk, eps, cov,
                   size(at_risk) > 0 AND size(at_risk) = size(cov) AS only,
                   g.status AS gap, g.reason AS gap_reason
        """, els=element_ids, eps=endpoints, min_conf=SETTINGS.link_review)
