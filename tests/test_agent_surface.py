"""Tests for the agent-facing honesty surface added on top of the pack.

* seeds resolve from ``file:line`` and from a qualified name, and an ambiguous
  seed yields a candidate list rather than a bare miss;
* a stale graph is refused by name (``definition_mismatch``), never served as
  the wrong function's body under the requested label;
* files edited since extraction are disclosed via the manifest hash;
* a heuristic ``tests`` edge can never be emitted as EXTRACTED (roadmap Phase
  0.5, which shipped the split but had no regression test pinning it).
"""

from __future__ import annotations

import hashlib
import json

from graphify_ext import context, graphio, symbols, test_link

SRC = '''\
def alpha(x):
    return beta(x)


def beta(y):
    return y * 2


class Widget:
    def build(self):
        return alpha(1)
'''


def _graph(tmp_path, *, alpha_line=1):
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "mod.py").write_text(SRC, encoding="utf-8")
    return {"nodes": [
        {"id": "pkg_mod", "label": "mod.py", "source_file": "pkg/mod.py",
         "source_location": "L1"},
        {"id": "pkg_mod_alpha", "label": "alpha()", "source_file": "pkg/mod.py",
         "source_location": f"L{alpha_line}", "_callable": True},
        {"id": "pkg_mod_beta", "label": "beta()", "source_file": "pkg/mod.py",
         "source_location": "L5", "_callable": True},
        {"id": "pkg_mod_widget", "label": "Widget", "source_file": "pkg/mod.py",
         "source_location": "L9"},
        {"id": "pkg_mod_widget_build", "label": ".build()", "source_file": "pkg/mod.py",
         "source_location": "L10", "_callable": True, "qualified_name": "Widget.build"},
    ], "links": [
        {"source": "pkg_mod_alpha", "target": "pkg_mod_beta", "relation": "calls"},
        {"source": "pkg_mod_widget", "target": "pkg_mod_widget_build", "relation": "method"},
    ]}


# --------------------------------------------------------------------------
# seed resolution
# --------------------------------------------------------------------------

def test_file_line_seed_resolves_to_enclosing_definition(tmp_path):
    d = _graph(tmp_path)
    assert graphio.resolve_node(d, "pkg/mod.py:6", root=tmp_path) == "pkg_mod_beta"
    assert graphio.resolve_node(d, "pkg/mod.py:L6", root=tmp_path) == "pkg_mod_beta"
    # the def line itself belongs to its own node
    assert graphio.resolve_node(d, "pkg/mod.py:5", root=tmp_path) == "pkg_mod_beta"


def test_file_line_past_end_of_file_resolves_to_nothing(tmp_path):
    d = _graph(tmp_path)
    assert graphio.resolve_node(d, "pkg/mod.py:9999", root=tmp_path) is None


def test_qualified_name_resolves_a_member(tmp_path):
    d = _graph(tmp_path)
    assert graphio.resolve_node(d, "Widget.build") == "pkg_mod_widget_build"


def test_candidates_rank_exact_then_bare_then_substring(tmp_path):
    d = _graph(tmp_path)
    d["nodes"].append({"id": "other_alpha", "label": "alpha()", "source_file": "o.py",
                       "source_location": "L3", "_callable": True})
    assert graphio.resolve_node(d, "alpha") is None          # now ambiguous
    rows = graphio.candidates(d, "alpha")
    assert [r["match"] for r in rows[:2]] == ["exact", "exact"]
    assert {r["id"] for r in rows[:2]} == {"pkg_mod_alpha", "other_alpha"}
    assert all("file" in r and "location" in r and "callable" in r for r in rows)
    sub = graphio.candidates(d, "wid")
    assert {r["id"] for r in sub} == {"pkg_mod_widget", "pkg_mod_widget_build"}
    assert graphio.candidates(d, "zzz_nothing") == []


def test_candidates_prefer_callables_at_equal_rank(tmp_path):
    d = _graph(tmp_path)
    d["nodes"].append({"id": "pkg_mod_rationale_1", "label": "alpha is documented here",
                       "source_file": "pkg/mod.py", "source_location": "L1"})
    rows = graphio.candidates(d, "alpha")
    assert rows[0]["id"] == "pkg_mod_alpha"
    assert rows[0]["callable"] is True


# --------------------------------------------------------------------------
# stale graph: refuse by name
# --------------------------------------------------------------------------

def test_definition_at_line_with_a_different_name_is_refused(tmp_path):
    """The graph says `alpha` is at L5; the file has `beta` there. Serving
    beta's body under the name alpha is the failure this guards against."""
    d = _graph(tmp_path, alpha_line=5)
    node = graphio.node_index(d)["pkg_mod_alpha"]
    got = symbols.resolve_node_detail(tmp_path, node)
    assert isinstance(got, symbols.Unresolved)
    assert got.code == symbols.DEFINITION_MISMATCH
    assert "beta" in got.detail and "alpha" in got.detail


def test_mismatch_surfaces_in_the_pack_as_unresolved_not_as_a_wrong_slice(tmp_path):
    d = _graph(tmp_path, alpha_line=5)
    pack = context.build_context(d, "pkg_mod_alpha", tmp_path, depth=1, budget=5000)
    assert not pack["seed_resolved"]
    assert pack["seed_unresolved_reason"] == symbols.DEFINITION_MISMATCH
    # beta itself is a legitimate neighbour and may appear under ITS OWN name;
    # what must never happen is beta's body appearing labelled as the seed.
    assert "pkg_mod_alpha" not in {i["id"] for i in pack["included"]}
    assert "alpha ===" not in pack["text"]
    seed_row = next(u for u in pack["unresolved"] if u["is_seed"])
    assert seed_row["reason_code"] == symbols.DEFINITION_MISMATCH


def test_resolve_without_a_label_still_returns_the_definition(tmp_path):
    """No `expect` means nothing to check against; the resolver must not
    refuse what it cannot dispute."""
    _graph(tmp_path)
    sym = symbols.resolve(tmp_path, "pkg/mod.py", 5)
    assert sym is not None and sym.name == "beta"


# --------------------------------------------------------------------------
# stale graph: disclose by file
# --------------------------------------------------------------------------

def _manifest_for(tmp_path, rel, *, correct=True):
    data = (tmp_path / rel).read_bytes()
    h = hashlib.md5(data, usedforsecurity=False).hexdigest()
    if not correct:
        h = "0" * 32
    return {rel: {"mtime": 0, "seen": 0, "ast_hash": h, "semantic_hash": h}}


def test_stale_files_empty_when_manifest_hash_matches(tmp_path):
    d = _graph(tmp_path)
    m = _manifest_for(tmp_path, "pkg/mod.py")
    pack = context.build_context(d, "pkg_mod_alpha", tmp_path, depth=1, budget=5000,
                                 manifest=m)
    assert pack["stale_check"] == "manifest"
    assert pack["stale_files"] == []


def test_edited_file_is_reported_stale(tmp_path):
    d = _graph(tmp_path)
    m = _manifest_for(tmp_path, "pkg/mod.py", correct=False)
    pack = context.build_context(d, "pkg_mod_alpha", tmp_path, depth=1, budget=5000,
                                 manifest=m)
    assert [s["file"] for s in pack["stale_files"]] == ["pkg/mod.py"]
    assert pack["stale_files"][0]["reason"] == "edited since extraction"
    assert pack["stale_files"][0]["manifest_hash"] == "0" * 32


def test_stale_check_is_skipped_not_faked_without_a_manifest(tmp_path):
    d = _graph(tmp_path)
    pack = context.build_context(d, "pkg_mod_alpha", tmp_path, depth=1, budget=5000)
    assert pack["stale_check"].startswith("skipped")
    assert pack["stale_files"] == []


def test_stale_check_covers_only_files_the_pack_touched(tmp_path):
    d = _graph(tmp_path)
    (tmp_path / "pkg" / "other.py").write_text("def z():\n    pass\n", encoding="utf-8")
    m = _manifest_for(tmp_path, "pkg/mod.py")
    m["pkg/other.py"] = {"ast_hash": "0" * 32}       # stale, but never shown
    pack = context.build_context(d, "pkg_mod_alpha", tmp_path, depth=1, budget=5000,
                                 manifest=m)
    assert pack["stale_files"] == []


# --------------------------------------------------------------------------
# Phase 0.5: test-edge provenance regression
# --------------------------------------------------------------------------

def test_heuristic_tests_edges_are_never_extracted():
    data = {"nodes": [
        {"id": "t", "label": "test_alpha()", "source_file": "tests/test_m.py",
         "source_location": "L1", "_callable": True},
        {"id": "p", "label": "alpha()", "source_file": "m.py",
         "source_location": "L1", "_callable": True},
    ], "links": []}
    found = test_link.heuristic(data)
    assert len(found["edges"]) == 1
    assert found["edges"][0]["confidence"] == "INFERRED"
    assert all(e["confidence"] != "EXTRACTED" for e in found["edges"])


def test_coverage_tests_edges_are_extracted_and_survive_injection(tmp_path):
    from graphify_ext import edge_inject
    (tmp_path / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    data = {"nodes": [
        {"id": "t_alpha", "label": "test_alpha()", "source_file": "tests/test_m.py",
         "source_location": "L1", "_callable": True},
        {"id": "m_alpha", "label": "alpha()", "source_file": "m.py",
         "source_location": "L1", "_callable": True},
    ], "links": []}
    out = tmp_path / "graphify-out"
    out.mkdir()
    gp = out / "graph.json"
    gp.write_text(json.dumps(data), encoding="utf-8")
    cov = {"files": {"m.py": {"contexts": {"2": ["tests/test_m.py::test_alpha|run"]}}}}
    findings = test_link.from_coverage(cov)
    assert findings["edges"][0]["confidence"] == "EXTRACTED"
    edge_inject.inject(gp, findings)
    edges = [e for e in graphio.edges(graphio.load(gp)) if e["relation"] == "tests"]
    assert len(edges) == 1 and edges[0]["confidence"] == "EXTRACTED"
    # and a heuristic edge injected into the same graph keeps ITS label
    edge_inject.inject(gp, test_link.heuristic(data))
    edges = [e for e in graphio.edges(graphio.load(gp)) if e["relation"] == "tests"]
    assert {e["confidence"] for e in edges} == {"INFERRED"}


# --------------------------------------------------------------------------
# file nodes are not failures
# --------------------------------------------------------------------------

def test_file_node_in_the_walk_is_reported_as_a_file_not_as_a_failure(tmp_path):
    """A containment walk reaches the FILE node. It has no body to slice, and
    reporting it as `no_definition_at_line` made every pack look like it had
    failed on something. It is a file; say so."""
    from graphify_ext import blast_radius as br
    d = _graph(tmp_path)
    d["links"].append({"source": "pkg_mod", "target": "pkg_mod_alpha", "relation": "contains"})
    rels = tuple(dict.fromkeys(br.DEFAULT_RELATIONS + br.MEMBER_RELATIONS))
    pack = context.build_context(d, "pkg_mod_alpha", tmp_path, depth=1, budget=5000,
                                 relations=rels)
    row = next(u for u in pack["unresolved"] if u["id"] == "pkg_mod")
    assert row["reason_code"] == "file_node"
    assert pack["seed_resolved"]
