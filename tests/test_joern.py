"""from_joern: Joern data flows -> taint edges with the full chain."""

from __future__ import annotations

import json

from graphify_ext import edge_inject, graphio

APP = '''\
def handler(req):
    raw = req.args["q"]
    return run(raw)


def run(v):
    cleaned = normalise(v)
    return execute(cleaned)


def normalise(v):
    return v.strip()


def execute(sql):
    return db.query(sql)
'''

NEUTRAL = {"flows": [{"rule": "sqli", "path": [
    {"file": "app.py", "line": 2, "method": "handler", "code": "req.args['q']"},
    {"file": "app.py", "line": 3, "method": "handler", "code": "run(raw)"},
    {"file": "app.py", "line": 7, "method": "run", "code": "normalise(v)"},
    {"file": "app.py", "line": 8, "method": "run", "code": "execute(cleaned)"},
    {"file": "app.py", "line": 16, "method": "execute", "code": "db.query(sql)"},
]}]}

# Joern's own toJson of a List[Path]: elements with filename/lineNumber
RAW = [{"elements": [
    {"filename": "app.py", "lineNumber": 2, "code": "req.args['q']"},
    {"filename": "app.py", "lineNumber": {"value": 16}, "code": "db.query(sql)"},
]}]


def _graph(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    nodes = [{"id": "app", "label": "app.py", "source_file": "app.py", "source_location": "L1"}]
    for name, line in (("handler", 1), ("run", 6), ("normalise", 11), ("execute", 15)):
        nodes.append({"id": f"app_{name}", "label": f"{name}()", "source_file": "app.py",
                      "source_location": f"L{line}", "_callable": True})
    out = tmp_path / "graphify-out"
    out.mkdir()
    gp = out / "graph.json"
    gp.write_text(json.dumps({"nodes": nodes, "links": []}), encoding="utf-8")
    return gp


def test_neutral_flow_yields_endpoints_and_chain(tmp_path):
    gp = _graph(tmp_path)
    findings = edge_inject.from_joern(NEUTRAL)
    assert findings["skipped"] == []
    rels = [e["relation"] for e in findings["edges"]]
    assert rels.count("reaches_sink") == 1
    assert all(e["confidence"] == "EXTERNAL" for e in findings["edges"])
    rep = edge_inject.inject(gp, findings)
    assert rep["unresolved"] == []
    edges = [e for e in graphio.edges(graphio.load(gp)) if e.get("origin") == "graphify-ext"]
    pairs = {(e["source"], e["target"], e["relation"]) for e in edges}
    # endpoints
    assert ("app_handler", "app_execute", "taints") in pairs
    assert ("app_handler", "app_execute", "reaches_sink") in pairs
    # the chain, collapsed to node granularity: handler -> run -> execute;
    # normalise is only CALLED from run, the flow never enters its body
    assert ("app_handler", "app_run", "taints") in pairs
    assert ("app_run", "app_execute", "taints") in pairs
    assert not any(s == t for s, t, _ in pairs)


def test_raw_joern_json_shape_is_accepted(tmp_path):
    gp = _graph(tmp_path)
    findings = edge_inject.from_joern(RAW, rule="cmdi")
    assert {e["relation"] for e in findings["edges"]} == {"taints", "reaches_sink"}
    assert all("cmdi" in e["detail"] for e in findings["edges"])
    rep = edge_inject.inject(gp, findings)
    assert rep["applied"] == 2 and rep["unresolved"] == []


def test_unlocated_flow_is_skipped_not_dropped_silently():
    findings = edge_inject.from_joern({"flows": [{"path": [{"code": "x"}]}]})
    assert findings["edges"] == []
    assert findings["skipped"] == [{"flow": 0, "reason": "fewer than two located elements"}]


def test_unresolvable_chain_element_is_reported_by_inject(tmp_path):
    gp = _graph(tmp_path)
    bad = {"flows": [{"path": [
        {"file": "app.py", "line": 2}, {"file": "missing.py", "line": 4}, {"file": "app.py", "line": 16}]}]}
    rep = edge_inject.inject(gp, edge_inject.from_joern(bad))
    reasons = {u["reason"] for u in rep["unresolved"]}
    assert reasons and all("unresolved" in r for r in reasons)
    assert rep["applied"] >= 2      # the endpoints still land
