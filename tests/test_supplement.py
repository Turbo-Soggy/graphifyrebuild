"""Tests for the supplement pass: definitions the extractor has no node for.

The properties that make this safe to run on someone's graph:

* it never touches an extractor node (ids, labels, edges byte-identical);
* it is idempotent -- apply twice, get the same graph;
* it materialises exactly the definitions that are missing, and declines the
  ones graphify omits on purpose (functions nested in functions);
* its `calls` edges are INFERRED and only ever emitted for an unambiguous
  name -- an ambiguous callee produces NOTHING, never a guess;
* it is inert until a slot opts in, so a stock-identical graph stays stock.
"""

from __future__ import annotations

import json

import pytest

from graphify_ext import graphio, supplement, symbols

EXPRESS_LIKE = '''\
var res = Object.create(http.ServerResponse.prototype);

res.status = function status(code) {
  this.statusCode = code;
  return this;
};

res.json = function json(obj) {
  var body = stringify(obj);
  this.set('Content-Type', 'application/json');
  return this.send(body);
};

res.send = function send(body) {
  function onfinish() { cleanup(); }
  return this.end(body);
};

function stringify(value) {
  return JSON.stringify(value);
}

module.exports = res;
'''

PY_OVERLOAD = '''\
import typing as t


@t.overload
def stream_with_context(fn: int) -> int: ...


@t.overload
def stream_with_context(fn: str) -> str: ...


def stream_with_context(fn):
    return helper(fn)


def helper(x):
    return x


class Adapter:
    def _get_connection(self, url):
        return url

    def get_connection(self, url):
        return self._get_connection(url)
'''


def _js_graph(tmp_path):
    """What graphify actually emits for EXPRESS_LIKE: the file node and the
    one declared function. Every `res.x = function` is absent (#1077 guard)."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "response.js").write_text(EXPRESS_LIKE, encoding="utf-8")
    return {"nodes": [
        {"id": "lib_response", "label": "response.js", "file_type": "code",
         "source_file": "lib/response.js", "source_location": "L1", "_origin": "ast"},
        {"id": "lib_response_stringify", "label": "stringify()", "file_type": "code",
         "source_file": "lib/response.js", "source_location": "L19",
         "_callable": True, "_origin": "ast"},
    ], "links": [
        {"source": "lib_response", "target": "lib_response_stringify",
         "relation": "contains", "confidence": "EXTRACTED"},
    ]}


def _py_graph(tmp_path):
    """graphify's output for PY_OVERLOAD: the first overload stub wins the id
    `m_stream_with_context`; the real body at L12 is gone. `get_connection`
    collides with `_get_connection` and is gone too (upstream #3302)."""
    (tmp_path / "m.py").write_text(PY_OVERLOAD, encoding="utf-8")
    return {"nodes": [
        {"id": "m", "label": "m.py", "file_type": "code", "source_file": "m.py",
         "source_location": "L1", "_origin": "ast"},
        {"id": "m_stream_with_context", "label": "stream_with_context()",
         "file_type": "code", "source_file": "m.py", "source_location": "L5",
         "_callable": True, "_origin": "ast"},
        {"id": "m_helper", "label": "helper()", "file_type": "code",
         "source_file": "m.py", "source_location": "L16", "_callable": True,
         "_origin": "ast"},
        {"id": "m_adapter", "label": "Adapter", "file_type": "code",
         "source_file": "m.py", "source_location": "L20", "_origin": "ast"},
        {"id": "m_adapter_get_connection", "label": "._get_connection()",
         "file_type": "code", "source_file": "m.py", "source_location": "L21",
         "_callable": True, "_origin": "ast"},
    ], "links": [
        {"source": "m", "target": "m_stream_with_context", "relation": "contains"},
        {"source": "m", "target": "m_helper", "relation": "contains"},
        {"source": "m", "target": "m_adapter", "relation": "contains"},
        {"source": "m_adapter", "target": "m_adapter_get_connection", "relation": "method"},
    ]}


# --------------------------------------------------------------------------
# what gets materialised
# --------------------------------------------------------------------------

def test_assignment_bound_js_members_get_nodes(tmp_path):
    data = _js_graph(tmp_path)
    res = supplement.compute(data, tmp_path)
    quals = {n["qualified_name"]: n for n in res["nodes"]}
    assert set(quals) == {"res.status", "res.json", "res.send"}
    j = quals["res.json"]
    assert j["label"] == "json()"
    assert j["source_location"] == "L8"          # the binder line graphify would record
    assert j["_callable"] is True
    assert j["origin"] == supplement.SUPPLEMENT_ORIGIN
    assert j["supplement_reason"] == supplement.REASON_MEMBER
    # owned by the file, via a contains edge the walk can follow
    owner = [e for e in res["edges"] if e["target"] == j["id"] and e["relation"] == "contains"]
    assert len(owner) == 1 and owner[0]["source"] == "lib_response"
    assert owner[0]["confidence"] == "EXTRACTED"


def test_nested_function_is_declined_not_materialised(tmp_path):
    """`onfinish` inside `res.send` is what graphify omits by design; the pack
    discloses it as unmodelled. Materialising it too would double-report and
    flood containment."""
    res = supplement.compute(_js_graph(tmp_path), tmp_path)
    assert "res.send.onfinish" not in {n["qualified_name"] for n in res["nodes"]}
    declined = [s for s in res["skipped"] if s["name"] == "res.send.onfinish"]
    assert len(declined) == 1 and "nested" in declined[0]["reason"]


def test_already_modelled_symbol_is_left_alone(tmp_path):
    data = _js_graph(tmp_path)
    before = json.dumps(data, sort_keys=True)
    res = supplement.compute(data, tmp_path)
    assert "stringify" not in {n["qualified_name"] for n in res["nodes"]}
    assert res["stats"]["already_modelled"] == 1
    assert json.dumps(data, sort_keys=True) == before, "compute must not mutate"


def test_id_collision_victims_get_a_line_suffixed_id(tmp_path):
    data = _py_graph(tmp_path)
    res = supplement.compute(data, tmp_path)
    by_q = {n["qualified_name"]: n for n in res["nodes"]}
    # the second overload stub AND the real body are both absent from the graph
    reals = [n for q, n in by_q.items() if q == "stream_with_context"]
    assert len(reals) == 1
    real = reals[0]
    assert real["source_location"] == "L12"
    assert real["id"] != "m_stream_with_context"           # never overwrite
    assert real["id"].startswith("m_stream_with_context_l")
    assert real["supplement_reason"] == supplement.REASON_COLLISION
    # get_connection is a METHOD of an existing class node
    gc = by_q["Adapter.get_connection"]
    assert gc["label"] == ".get_connection()"
    owner = [e for e in res["edges"] if e["target"] == gc["id"]]
    assert [(e["source"], e["relation"]) for e in owner
            if e["relation"] in ("method", "contains")] == [("m_adapter", "method")]


def test_second_overload_stub_is_also_materialised_distinctly(tmp_path):
    """Two overload stubs share a name; the graph kept one. The other stub is
    a real definition in the source and gets its own node -- a node per
    definition, not a node per name."""
    res = supplement.compute(_py_graph(tmp_path), tmp_path)
    lines = sorted(n["source_location"] for n in res["nodes"]
                   if n["qualified_name"] == "stream_with_context")
    assert lines == ["L12", "L9"]


# --------------------------------------------------------------------------
# calls: INFERRED, unambiguous only
# --------------------------------------------------------------------------

def test_calls_from_a_new_node_resolve_within_the_file(tmp_path):
    res = supplement.compute(_js_graph(tmp_path), tmp_path)
    ids = {n["qualified_name"]: n["id"] for n in res["nodes"]}
    calls = {(e["source"], e["target"]) for e in res["edges"] if e["relation"] == "calls"}
    assert (ids["res.json"], "lib_response_stringify") in calls   # new -> existing
    assert (ids["res.json"], ids["res.send"]) in calls            # new -> new
    for e in res["edges"]:
        if e["relation"] == "calls":
            assert e["confidence"] == "INFERRED"
            assert e["origin"] == supplement.SUPPLEMENT_ORIGIN
            assert e["detail"].startswith("supplement:name-match")


def test_ambiguous_callee_emits_nothing(tmp_path):
    """Two callables named `helper` anywhere the resolver would look -> no edge.
    A wrong `calls` edge is the failure mode the whole layer exists to avoid."""
    (tmp_path / "a.py").write_text(
        "def helper(x):\n    return x\n\ndef caller():\n    return helper(1)\n",
        encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "def helper(y):\n    return y\n", encoding="utf-8")
    data = {"nodes": [
        {"id": "a", "label": "a.py", "source_file": "a.py", "source_location": "L1"},
        {"id": "b", "label": "b.py", "source_file": "b.py", "source_location": "L1"},
        {"id": "a_helper", "label": "helper()", "source_file": "a.py",
         "source_location": "L1", "_callable": True},
        {"id": "b_helper", "label": "helper()", "source_file": "b.py",
         "source_location": "L1", "_callable": True},
        # `caller` is deliberately missing so the supplement creates it
    ], "links": []}
    # make the file-local lookup ambiguous too: a second `helper` in a.py
    (tmp_path / "a.py").write_text(
        "def helper(x):\n    return x\n\ndef caller():\n    return helper(1)\n\n"
        "class K:\n    def helper(self):\n        return 2\n", encoding="utf-8")
    data["nodes"].append({"id": "a_k", "label": "K", "source_file": "a.py",
                          "source_location": "L7"})
    data["nodes"].append({"id": "a_k_helper", "label": ".helper()", "source_file": "a.py",
                          "source_location": "L8", "_callable": True})
    res = supplement.compute(data, tmp_path)
    assert [n["qualified_name"] for n in res["nodes"]] == ["caller"]
    assert not [e for e in res["edges"] if e["relation"] == "calls"]


def test_existing_to_existing_calls_are_not_reinvented(tmp_path):
    """`stringify` -> `JSON.stringify` and every extractor-to-extractor pair is
    the extractor's business; the supplement only adds edges touching a node
    it created."""
    res = supplement.compute(_js_graph(tmp_path), tmp_path)
    new_ids = {n["id"] for n in res["nodes"]}
    for e in res["edges"]:
        assert e["source"] in new_ids or e["target"] in new_ids


# --------------------------------------------------------------------------
# apply / strip / idempotency / opt-in
# --------------------------------------------------------------------------

def _write_graph(tmp_path, data):
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps(data), encoding="utf-8")
    return out / "graph.json"


def test_apply_is_idempotent_and_preserves_extractor_nodes(tmp_path):
    data = _js_graph(tmp_path)
    gp = _write_graph(tmp_path, data)
    stock_nodes = {n["id"]: n for n in data["nodes"]}
    stock_edges = sorted(json.dumps(e, sort_keys=True) for e in data["links"])

    rep1 = supplement.apply(gp, root=tmp_path)
    once = graphio.load(gp)
    rep2 = supplement.apply(gp, root=tmp_path)
    twice = graphio.load(gp)

    assert rep1["added_nodes"] == 3 and rep2["added_nodes"] == 3
    assert rep2["removed_previous_nodes"] == 3
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)
    for nid, n in stock_nodes.items():
        assert {k: v for k, v in graphio.node_index(twice)[nid].items()} == n
    kept = sorted(json.dumps(e, sort_keys=True) for e in graphio.edges(twice)
                  if e.get("origin") != supplement.SUPPLEMENT_ORIGIN)
    assert kept == stock_edges


def test_strip_removes_everything_including_edges_to_removed_nodes(tmp_path):
    data = _js_graph(tmp_path)
    gp = _write_graph(tmp_path, data)
    supplement.apply(gp, root=tmp_path)
    d = graphio.load(gp)
    # an external edge that points at a supplemented node must go too, or the
    # graph would hold a dangling reference after the strip
    d["links"].append({"source": "lib_response_stringify",
                       "target": graphio.resolve_node(d, "res.json"),
                       "relation": "tests", "origin": "graphify-ext"})
    n, e = supplement.strip(d)
    assert n == 3
    ids = set(graphio.node_index(d))
    assert all(x["source"] in ids and x["target"] in ids for x in d["links"])


def test_reapply_is_inert_until_the_slot_opts_in(tmp_path):
    gp = _write_graph(tmp_path, _js_graph(tmp_path))
    before = gp.read_text(encoding="utf-8")
    assert supplement.reapply(gp.parent) is None
    assert gp.read_text(encoding="utf-8") == before
    supplement.enable(gp.parent)
    rep = supplement.reapply(gp.parent)
    assert rep is not None and rep["added_nodes"] == 3
    assert supplement.is_enabled(gp.parent)


def test_external_edge_strip_does_not_remove_supplement_edges(tmp_path):
    """edge_inject strips by its OWN origin; supplement edges carry a different
    one and must survive a re-inject, or every inject would silently undo the
    supplement."""
    from graphify_ext import edge_inject
    gp = _write_graph(tmp_path, _js_graph(tmp_path))
    supplement.apply(gp, root=tmp_path)
    n_sup = sum(1 for e in graphio.edges(graphio.load(gp))
                if e.get("origin") == supplement.SUPPLEMENT_ORIGIN)
    edge_inject.inject(gp, {"edges": []})
    after = sum(1 for e in graphio.edges(graphio.load(gp))
                if e.get("origin") == supplement.SUPPLEMENT_ORIGIN)
    assert after == n_sup > 0


# --------------------------------------------------------------------------
# downstream: seeds resolve, context slices, blast radius walks
# --------------------------------------------------------------------------

def test_supplemented_member_is_a_usable_seed_end_to_end(tmp_path):
    from graphify_ext import blast_radius as br
    from graphify_ext import context
    gp = _write_graph(tmp_path, _js_graph(tmp_path))
    supplement.apply(gp, root=tmp_path)
    d = graphio.load(gp)
    nid = graphio.resolve_node(d, "res.json")
    assert nid is not None
    assert graphio.resolve_node(d, "lib/response.js:9") == nid   # file:line
    rels = tuple(dict.fromkeys(br.DEFAULT_RELATIONS + br.MEMBER_RELATIONS))
    pack = context.build_context(d, nid, tmp_path, depth=2, budget=6000,
                                 relations=rels)
    assert pack["seed_resolved"]
    seed = pack["included"][0]
    assert seed["id"] == nid and seed["origin"] == supplement.SUPPLEMENT_ORIGIN
    assert seed["qualified_name"] == "res.json"
    assert "return this.send(body)" in pack["text"]
    names = {i["name"] for i in pack["included"]}
    assert {"send", "stringify"} <= names, names       # callees came along
    # nothing in the shown code is left undisclosed except the nested helper
    assert [u["name"] for u in pack["unmodelled"]] == ["res.send.onfinish"]


# --------------------------------------------------------------------------
# nesting is about extents, not name segments
# --------------------------------------------------------------------------

ROUTER_LIKE = '''\
var proto = module.exports = function (options) {
  function router(req, res, next) { router.handle(req, res, next); }
  return router;
};

proto.param = function param(name, fn) {
  this.params[name] = fn;
};
'''


def test_property_assigned_onto_a_function_object_is_not_nested(tmp_path):
    """`proto.param` has a function ancestor by NAME but sits outside its body.
    express binds its whole router API this way; the old kind-only test
    declined every one of them as "nested" (task express/1e2951a8 unscoreable)."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "router.js").write_text(ROUTER_LIKE, encoding="utf-8")
    defs = symbols.definitions_from_source(ROUTER_LIKE.encode(), "lib/router.js")
    kinds = {s.name: s.kind for s in defs}
    extents = {s.name: (s.start, s.end) for s in defs}
    param = next(s for s in defs if s.name == "proto.param")
    inner = next(s for s in defs if s.name.endswith(".router"))
    assert not symbols.is_nested_in_function("proto.param", kinds, param.def_line, extents)
    assert symbols.is_nested_in_function(inner.name, kinds, inner.def_line, extents)

    data = {"nodes": [
        {"id": "lib_router", "label": "router.js", "source_file": "lib/router.js",
         "source_location": "L1"},
        {"id": "lib_router_proto", "label": "proto()", "source_file": "lib/router.js",
         "source_location": "L1", "_callable": True},
    ], "links": []}
    res = supplement.compute(data, tmp_path)
    assert "proto.param" in {n["qualified_name"] for n in res["nodes"]}
    assert inner.name not in {n["qualified_name"] for n in res["nodes"]}


# --------------------------------------------------------------------------
# stale files are refused whole
# --------------------------------------------------------------------------

def test_stale_file_is_refused_by_structural_tell(tmp_path):
    """The graph says a callable begins at L3; the parser finds no definition
    there. That only happens after an edit shifted lines, and supplementing
    such a file would duplicate every shifted definition under a new id."""
    (tmp_path / "m.py").write_text(
        "import os\n\n\ndef alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
        encoding="utf-8")
    data = {"nodes": [
        {"id": "m", "label": "m.py", "source_file": "m.py", "source_location": "L1"},
        {"id": "m_alpha", "label": "alpha()", "source_file": "m.py",
         "source_location": "L3", "_callable": True},       # really at L4 now
    ], "links": []}
    res = supplement.compute(data, tmp_path)
    assert res["nodes"] == []
    assert [s["file"] for s in res["stale_files"]] == ["m.py"]
    assert "no definition begins" in res["stale_files"][0]["reason"]
    assert res["stats"]["files_stale"] == 1


def test_stale_file_is_refused_by_manifest_hash(tmp_path):
    (tmp_path / "m.py").write_text("def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
                                   encoding="utf-8")
    data = {"nodes": [
        {"id": "m", "label": "m.py", "source_file": "m.py", "source_location": "L1"},
        {"id": "m_alpha", "label": "alpha()", "source_file": "m.py",
         "source_location": "L1", "_callable": True},
    ], "links": []}
    fresh = supplement.compute(data, tmp_path, manifest=None)
    assert [n["qualified_name"] for n in fresh["nodes"]] == ["beta"]
    stale = supplement.compute(data, tmp_path, manifest={"m.py": {"ast_hash": "0" * 32}})
    assert stale["nodes"] == []
    assert "manifest hash differs" in stale["stale_files"][0]["reason"]


def test_apply_reads_the_manifest_next_to_the_graph(tmp_path):
    (tmp_path / "m.py").write_text("def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
                                   encoding="utf-8")
    data = {"nodes": [
        {"id": "m", "label": "m.py", "source_file": "m.py", "source_location": "L1"},
        {"id": "m_alpha", "label": "alpha()", "source_file": "m.py",
         "source_location": "L1", "_callable": True},
    ], "links": []}
    gp = _write_graph(tmp_path, data)
    (gp.parent / "manifest.json").write_text(json.dumps({"m.py": {"ast_hash": "0" * 32}}),
                                             encoding="utf-8")
    rep = supplement.apply(gp, root=tmp_path)
    assert rep["added_nodes"] == 0
    assert rep["stale_files"] and rep["stale_files"][0]["file"] == "m.py"


def test_index_file_labelled_with_its_directory_is_still_a_file_node(tmp_path):
    """graphify labels `lib/router/index.js` as `router/index.js`. Basename-only
    matching missed every such file (express router: 0 of 12 members added)."""
    (tmp_path / "lib" / "router").mkdir(parents=True)
    (tmp_path / "lib" / "router" / "index.js").write_text(ROUTER_LIKE, encoding="utf-8")
    data = {"nodes": [
        {"id": "lib_router_index", "label": "router/index.js",
         "source_file": "lib/router/index.js", "source_location": "L1"},
        {"id": "lib_router_index_proto", "label": "proto()",
         "source_file": "lib/router/index.js", "source_location": "L1", "_callable": True},
    ], "links": []}
    assert graphio.is_file_node(data["nodes"][0])
    assert not graphio.is_file_node(data["nodes"][1])
    res = supplement.compute(data, tmp_path)
    assert "proto.param" in {n["qualified_name"] for n in res["nodes"]}
