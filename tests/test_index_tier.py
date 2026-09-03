"""Tests for the two-tier pack, class summaries, related tests and refresh.

* the index tier names what the body tier could not show, within its own
  share of the SAME total budget, and never silently drops anything;
* a RELATED class is a signature + member index, a SEED class is a body;
* test files touching anything shown are listed with relation + confidence;
* refresh does nothing when nothing is stale, and detects stale files from
  the manifest.
"""

from __future__ import annotations

import hashlib
import json

from graphify_ext import blast_radius as br
from graphify_ext import context, graphio, refresh

_RELS = tuple(dict.fromkeys(br.DEFAULT_RELATIONS + br.MEMBER_RELATIONS))

SRC = "\n".join(
    [f"def f{i}(a, b):\n    total = a + b\n    for _ in range(3):\n"
     f"        total += a * b\n    return total\n" for i in range(12)]
) + "\n\nclass Big:\n    \"\"\"doc\"\"\"\n\n    def m1(self):\n        return 1\n\n    def m2(self):\n        return 2\n"


def _chain_graph(tmp_path):
    """seed -> f1..f5 (depth 1) -> f6..f11 (depth 2) -> Big (depth 3)."""
    (tmp_path / "m.py").write_text(SRC, encoding="utf-8")
    lines = {}
    for i, ln in enumerate(l + 1 for l, t in enumerate(SRC.splitlines()) if t.startswith("def f")):
        lines[f"f{i}"] = ln
    big_line = next(l + 1 for l, t in enumerate(SRC.splitlines()) if t.startswith("class Big"))
    nodes = [{"id": "f0", "label": "f0()", "source_file": "m.py",
              "source_location": f"L{lines['f0']}", "_callable": True}]
    links = []
    for i in range(1, 12):
        nodes.append({"id": f"f{i}", "label": f"f{i}()", "source_file": "m.py",
                      "source_location": f"L{lines[f'f{i}']}", "_callable": True})
    for i in range(1, 6):
        links.append({"source": "f0", "target": f"f{i}", "relation": "calls"})
    for i in range(6, 12):
        links.append({"source": f"f{(i - 6) % 5 + 1}", "target": f"f{i}", "relation": "calls"})
    nodes.append({"id": "big", "label": "Big", "source_file": "m.py",
                  "source_location": f"L{big_line}"})
    links.append({"source": "f11", "target": "big", "relation": "references"})
    return {"nodes": nodes, "links": links}, lines, big_line


def test_index_tier_names_the_overflow_and_the_extra_hop(tmp_path):
    graph, lines, big_line = _chain_graph(tmp_path)
    # bodies only, depth 2: the depth-3 class is not reached at all
    flat = context.build_context(graph, "f0", tmp_path, depth=2, budget=6000,
                                 relations=_RELS, index_budget=0)
    assert flat["index"] == [] and flat["index_depth"] == 2
    ids_flat = {i["id"] for i in flat["included"]}
    assert "big" not in ids_flat

    # same total budget, part of it carved out for the index: the walk goes one
    # hop further and the class appears in the index, never as a body
    tiered = context.build_context(graph, "f0", tmp_path, depth=2, budget=6000,
                                   relations=_RELS, index_budget=1500)
    assert tiered["index_depth"] == 3
    idx = {i["id"]: i for i in tiered["index"]}
    assert "big" in idx and idx["big"]["tier_reason"] == "depth"
    assert idx["big"]["def_line"] == big_line
    assert "big" not in {i["id"] for i in tiered["included"]}
    assert tiered["tokens_used"] <= 6000
    assert f"m.py:L{big_line}" in tiered["text"]
    assert "--- index:" in tiered["text"]


def test_body_overflow_falls_into_the_index_before_being_omitted(tmp_path):
    graph, lines, _ = _chain_graph(tmp_path)
    # tiny body budget: only the seed and ~one body fit; the rest must be NAMED
    pack = context.build_context(graph, "f0", tmp_path, depth=1, budget=900,
                                 relations=_RELS, index_budget=600)
    bodies = {i["id"] for i in pack["included"]}
    named = {i["id"] for i in pack["index"]}
    dropped = {o["id"] for o in pack["omitted"]}
    assert "f0" in bodies
    d1 = {f"f{i}" for i in range(1, 6)}
    named_d1 = {i["id"] for i in pack["index"] if i["id"] in d1}
    assert named_d1, "overflow must be named, not dropped, while index budget remains"
    assert all(i["tier_reason"] == "budget" for i in pack["index"] if i["id"] in d1)
    assert all(i["tier_reason"] == "depth" for i in pack["index"] if i["id"] not in d1)
    # accounting is complete: every depth-1 node is in exactly one bucket
    assert (bodies | named | dropped) >= d1 | {"f0"}
    assert not (bodies & named) and not (named & dropped)


def test_index_budget_zero_reproduces_the_single_tier_pack(tmp_path):
    graph, _, _ = _chain_graph(tmp_path)
    a = context.build_context(graph, "f0", tmp_path, depth=2, budget=2000,
                              relations=_RELS, index_budget=0)
    assert a["index"] == []
    assert all(o["reason"] == "budget" for o in a["omitted"])


def test_related_class_is_a_summary_and_seed_class_is_a_body(tmp_path):
    graph, _, big_line = _chain_graph(tmp_path)
    graph["links"].append({"source": "f0", "target": "big", "relation": "references"})
    pack = context.build_context(graph, "f0", tmp_path, depth=1, budget=6000,
                                 relations=_RELS, index_budget=0)
    big = next(i for i in pack["included"] if i["id"] == "big")
    assert big["kind"] == "class"
    text = pack["text"]
    assert "2 member(s); bodies are separate symbols" in text
    assert "def m1(self)" in text          # member signature listed ...
    assert "return 1" not in text          # ... but the body is not
    seed_pack = context.build_context(graph, "big", tmp_path, depth=0, budget=6000,
                                      relations=_RELS, index_budget=0)
    assert "return 1" in seed_pack["text"]  # the seed class keeps its body


def test_related_tests_are_listed_with_relation_and_confidence(tmp_path):
    graph, lines, _ = _chain_graph(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text(
        "def test_f0():\n    assert f0(1, 2)\n", encoding="utf-8")
    graph["nodes"] += [
        {"id": "tests_test_m", "label": "test_m.py", "source_file": "tests/test_m.py",
         "source_location": "L1"},
        {"id": "t_f0", "label": "test_f0()", "source_file": "tests/test_m.py",
         "source_location": "L1", "_callable": True},
    ]
    graph["links"] += [
        {"source": "t_f0", "target": "f0", "relation": "calls", "confidence": "EXTRACTED"},
        {"source": "tests_test_m", "target": "f0", "relation": "imports_from"},
        {"source": "t_f0", "target": "f1", "relation": "tests", "confidence": "INFERRED",
         "detail": "heuristic:name-match"},
    ]
    pack = context.build_context(graph, "f0", tmp_path, depth=1, budget=6000,
                                 relations=_RELS, index_budget=0)
    rt = pack["related_tests"]
    kinds = {(r["test_id"], r["touches"], r["relation"]): r for r in rt}
    assert ("t_f0", "f0", "calls") in kinds
    assert ("tests_test_m", "f0", "imports_from") in kinds
    assert kinds[("t_f0", "f1", "tests")]["confidence"] == "INFERRED"
    # seed's tests come first
    assert rt[0]["touches"] == "f0"
    # a test the walk itself reached (it calls the seed) is shown AND listed
    assert "t_f0" in {i["id"] for i in pack["included"]}


def test_refresh_reports_nothing_stale_when_hashes_match(tmp_path):
    (tmp_path / "m.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
    h = hashlib.md5((tmp_path / "m.py").read_bytes(), usedforsecurity=False).hexdigest()
    (out / "manifest.json").write_text(json.dumps({"m.py": {"ast_hash": h}}), encoding="utf-8")
    assert refresh.stale_paths(out, tmp_path) == []
    rep = refresh.refresh(out, root=tmp_path)
    assert rep["ok"] and rep["updated"] is False and rep["paths"] == []


def test_refresh_detects_edited_and_deleted_files(tmp_path):
    (tmp_path / "m.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({
        "m.py": {"ast_hash": "0" * 32},        # edited since
        "gone.py": {"ast_hash": "1" * 32},     # deleted since
    }), encoding="utf-8")
    assert refresh.stale_paths(out, tmp_path) == ["gone.py", "m.py"]


def test_refresh_refuses_without_a_graph(tmp_path):
    rep = refresh.refresh(tmp_path / "graphify-out", root=tmp_path)
    assert rep["ok"] is False and "no graph" in rep["error"]


def test_dynamic_index_spends_what_the_bodies_left_over(tmp_path):
    """`index_budget` is a reserve: bodies may use up to budget - reserve, and
    the index then gets budget - used, never less than the reserve. With a
    fixed split the same pack would have wasted the bodies' unspent share."""
    graph, _, _ = _chain_graph(tmp_path)
    fixed = context.build_context(graph, "f0", tmp_path, depth=1, budget=1400,
                                  relations=_RELS, index_budget=200, index_dynamic=False)
    dyn = context.build_context(graph, "f0", tmp_path, depth=1, budget=1400,
                                relations=_RELS, index_budget=200, index_dynamic=True)
    assert fixed["index_cap"] == 200
    assert dyn["index_cap"] >= 200
    assert len(dyn["index"]) >= len(fixed["index"])
    assert dyn["tokens_used"] <= 1400 and fixed["tokens_used"] <= 1400
    # the body tier is identical: the reserve is what bounds bodies in both modes
    assert [i["id"] for i in dyn["included"]] == [i["id"] for i in fixed["included"]]


def test_mention_first_promotes_symbols_the_seed_names(tmp_path):
    """A depth-2 symbol whose name appears in the seed's source outranks a
    depth-1 symbol the seed never mentions. Under order="current" depth wins."""
    src = ("def seed():\n    return deep_helper()\n\n\n"
           "def near():\n    return 1\n\n\n"
           "def deep_helper():\n    return 2\n")
    (tmp_path / "m.py").write_text(src, encoding="utf-8")
    graph = {"nodes": [
        {"id": "seed", "label": "seed()", "source_file": "m.py", "source_location": "L1"},
        {"id": "near", "label": "near()", "source_file": "m.py", "source_location": "L5"},
        {"id": "deep", "label": "deep_helper()", "source_file": "m.py", "source_location": "L9"},
    ], "links": [
        {"source": "seed", "target": "near", "relation": "calls"},
        {"source": "near", "target": "deep", "relation": "calls"},   # deep is depth 2
    ]}
    shipped = [i["id"] for i in context.build_context(
        graph, "seed", tmp_path, depth=2, budget=6000, relations=_RELS)["included"]]
    current = [i["id"] for i in context.build_context(
        graph, "seed", tmp_path, depth=2, budget=6000, relations=_RELS,
        order="current")["included"]]
    assert shipped.index("deep") < shipped.index("near")
    assert current.index("near") < current.index("deep")


def test_review_checklist_lists_call_sites_and_siblings_with_shown_flag(tmp_path):
    """The two things "stopped one symbol short" means: callers of the seed and
    members of its owner -- listed even when the pack did not show them."""
    graph, lines, big_line = _chain_graph(tmp_path)
    # f3 calls f0 (a call site); Big owns f0 and f4 (siblings), f4 never shown
    graph["links"] += [
        {"source": "f3", "target": "f0", "relation": "calls", "source_location": "L99"},
        {"source": "big", "target": "f0", "relation": "method"},
        {"source": "big", "target": "f4", "relation": "method"},
    ]
    pack = context.build_context(graph, "f0", tmp_path, depth=1, budget=700,
                                 relations=_RELS, index_budget=0)
    rc = pack["review_checklist"]
    cs = {c["id"]: c for c in rc["call_sites_of_seed"]}
    assert "f3" in cs and cs["f3"]["call_line"] == "L99"
    sib = {m["id"]: m for m in rc["sibling_members"]}
    assert set(sib) == {"f4"} and sib["f4"]["owner"] == "Big"
    shown = {i["id"] for i in pack["included"]}
    assert all(c["shown"] == (c["id"] in shown) for c in rc["call_sites_of_seed"])
    assert "call site" in rc["note"]
