"""Tests for symbol extent resolution and the context pack.

These cover the property that makes the feature safe to trust: a slice is either
*exactly* the symbol's source, or it is not emitted at all. An approximately
right slice is the dangerous outcome, because it looks as authoritative as a
correct one, so several tests assert the *absence* of a guess rather than the
presence of an answer.
"""

from __future__ import annotations

import json

import pytest

from graphify_ext import context, symbols

SRC = '''\
import os


def module_level(a, b=2):
    """Doc."""
    return a + b


class Widget:
    """A widget."""

    CONST = 1

    def __init__(self, name):
        self.name = name

    @property
    def label(self):
        return self.name.upper()

    @staticmethod
    def build(spec: dict) -> "Widget":
        return Widget(spec["name"])

    async def refresh(self, *, force=False):
        return await _fetch(self.name, force)


async def _fetch(name, force):
    return name
'''


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(SRC, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# extents
# --------------------------------------------------------------------------

def test_extent_is_exact_not_approximate(tree):
    sym = symbols.resolve(tree, "pkg/mod.py", 4)
    assert sym.name == "module_level"
    assert (sym.start, sym.end) == (4, 6)
    # The slice must be the function and nothing after it: the blank lines and
    # the following class are what a "next symbol's start - 1" guess would drag in.
    assert sym.source.rstrip().endswith("return a + b")
    assert "class Widget" not in sym.source


def test_decorated_symbol_includes_its_decorator(tree):
    # graphify records the `def` line (18), not the decorator line (17). A slice
    # that started at the recorded line would silently drop @property, which
    # changes what the symbol *is*.
    sym = symbols.resolve(tree, "pkg/mod.py", 18)
    assert sym.def_line == 18
    assert sym.start == 17
    assert sym.source.splitlines()[0].strip() == "@property"
    assert sym.signature == "def label(self)"


def test_class_extent_spans_all_members(tree):
    sym = symbols.resolve(tree, "pkg/mod.py", 9)
    assert sym.kind == "class"
    assert sym.start == 9
    assert "async def refresh" in sym.source
    assert "async def _fetch" not in sym.source  # stops at the class, not after it


def test_async_and_keyword_only_signature(tree):
    sym = symbols.resolve(tree, "pkg/mod.py", 25)
    assert sym.name == "refresh"
    assert sym.signature == "async def refresh(self, *, force=False)"


def test_signature_is_the_def_clause_decorators_stay_in_source(tree):
    # The signature is the call contract an agent needs to honour; decorators
    # change behaviour but are not part of it, so they belong to `source`.
    # Asserting both keeps the split explicit rather than incidental.
    sym = symbols.resolve(tree, "pkg/mod.py", 22)
    assert sym.signature == 'def build(spec: dict) -> "Widget"'
    assert sym.source.splitlines()[0].strip() == "@staticmethod"


# --------------------------------------------------------------------------
# refusal to guess
# --------------------------------------------------------------------------

def test_line_with_no_definition_resolves_to_nothing(tree):
    # L5 is the docstring inside module_level. graphify emits doc/"rationale"
    # nodes at exactly such lines, and they must NOT be handed back as if they
    # were the enclosing function.
    assert symbols.resolve(tree, "pkg/mod.py", 5) is None


def test_unsupported_language_reports_none_rather_than_guessing(tree):
    (tree / "pkg" / "thing.rb").write_text("def foo\nend\n", encoding="utf-8")
    assert symbols.language_for("pkg/thing.rb") is None
    assert symbols.resolve(tree, "pkg/thing.rb", 1) is None


def test_missing_file_resolves_to_nothing(tree):
    assert symbols.resolve(tree, "pkg/absent.py", 1) is None


# --------------------------------------------------------------------------
# context pack
# --------------------------------------------------------------------------

def _graph() -> dict:
    return {
        "nodes": [
            {"id": "seed", "label": ".label()", "source_file": "pkg/mod.py",
             "source_location": "L18"},
            {"id": "callee", "label": "module_level()", "source_file": "pkg/mod.py",
             "source_location": "L4"},
            {"id": "doc", "label": "A widget.", "source_file": "pkg/mod.py",
             "source_location": "L10"},
        ],
        "links": [
            {"source": "seed", "target": "callee", "relation": "calls"},
            {"source": "seed", "target": "doc", "relation": "calls"},
        ],
    }


def test_pack_returns_actual_source_for_seed_and_neighbour(tree):
    pack = context.build_context(_graph(), "seed", tree, depth=1, budget=5000)
    names = {i["name"] for i in pack["included"]}
    assert names == {"label", "module_level"}
    assert "return self.name.upper()" in pack["text"]
    assert "return a + b" in pack["text"]


def test_unresolvable_neighbour_is_reported_not_dropped(tree):
    pack = context.build_context(_graph(), "seed", tree, depth=1, budget=5000)
    assert [u["id"] for u in pack["unresolved"]] == ["doc"]
    # Assert the stable machine-readable code, not the prose: callers branch on
    # `reason_code`, and pinning wording would make rewording a false failure.
    assert pack["unresolved"][0]["reason_code"] == symbols.NO_DEFINITION_AT_LINE
    assert pack["unresolved"][0]["is_seed"] is False


def test_budget_omits_but_never_silently(tree):
    # A budget that fits the seed alone: the neighbour must appear in `omitted`
    # with its location, so the agent knows what it has not been shown.
    pack = context.build_context(_graph(), "seed", tree, depth=1, budget=40)
    assert [i["id"] for i in pack["included"]] == ["seed"]
    assert [o["id"] for o in pack["omitted"]] == ["callee"]
    assert pack["omitted"][0]["reason"] == "budget"


def test_seed_is_always_included_even_over_budget(tree):
    pack = context.build_context(_graph(), "seed", tree, depth=1, budget=1)
    assert [i["id"] for i in pack["included"]] == ["seed"]
    assert "return self.name.upper()" in pack["text"]


def test_per_symbol_cap_truncates_explicitly(tree):
    pack = context.build_context(_graph(), "seed", tree, depth=1, budget=5000,
                                 per_symbol_cap=1)
    assert pack["included"][0]["truncated"] is True
    assert "truncated at 1 of" in pack["text"]
    # The omitted range is stated, not implied.
    assert "omitted)" in pack["text"]


def test_def_line_is_the_join_key_back_to_the_graph(tree):
    # `lines[0]` spans decorators and therefore does NOT match graphify's
    # source_location; def_line does. Anything joining on the wrong one would
    # mis-attribute every decorated symbol.
    pack = context.build_context(_graph(), "seed", tree, depth=1, budget=5000)
    seed = next(i for i in pack["included"] if i["id"] == "seed")
    assert seed["lines"][0] == 17 and seed["def_line"] == 18


def test_token_method_is_disclosed(tree):
    pack = context.build_context(_graph(), "seed", tree, depth=1, budget=5000)
    assert pack["token_method"] in ("tiktoken/cl100k_base", "approx-chars/4")
    assert pack["tokens_used"] > 0


# --------------------------------------------------------------------------
# the refusal must be legible, not just present
# --------------------------------------------------------------------------

def test_each_failure_mode_reports_its_own_code(tree):
    """Five distinct causes must not collapse into one message.

    Before this was fixed, every failure returned a bare None and the caller
    inferred a reason — so an unreadable file was reported to the agent as
    "probably a docstring node", which is a guess presented as a refusal.
    """
    # unsupported language
    (tree / "pkg" / "thing.rb").write_text("def foo\nend\n", encoding="utf-8")
    u = symbols.resolve_detail(tree, "pkg/thing.rb", 1)
    assert u.code == symbols.UNSUPPORTED_LANGUAGE

    # file that is not there
    u = symbols.resolve_detail(tree, "pkg/absent.py", 1)
    assert u.code == symbols.FILE_UNREADABLE
    assert u.file == "pkg/absent.py" and u.def_line == 1

    # a real file, but the line holds no definition (a docstring line)
    u = symbols.resolve_detail(tree, "pkg/mod.py", 5)
    assert u.code == symbols.NO_DEFINITION_AT_LINE

    # graph node with a malformed source_location
    u = symbols.resolve_node_detail(
        tree, {"source_file": "pkg/mod.py", "source_location": "line 4"})
    assert u.code == symbols.BAD_SOURCE_LOCATION

    # graph node with no source_file at all
    u = symbols.resolve_node_detail(tree, {"source_location": "L4"})
    assert u.code == symbols.MISSING_SOURCE_FILE


def test_resolve_never_returns_a_bare_none_from_detail(tree):
    """`resolve_detail` is total: always a Symbol or a typed Unresolved."""
    for path, line in (("pkg/mod.py", 4), ("pkg/mod.py", 5),
                       ("pkg/absent.py", 1), ("pkg/thing.rb", 1)):
        got = symbols.resolve_detail(tree, path, line)
        assert isinstance(got, (symbols.Symbol, symbols.Unresolved))
        assert got is not None


def test_context_reports_the_real_reason_not_an_inferred_one(tree):
    graph = {
        "nodes": [
            {"id": "seed", "label": ".label()", "source_file": "pkg/mod.py",
             "source_location": "L18"},
            {"id": "gone", "label": "x()", "source_file": "pkg/absent.py",
             "source_location": "L1"},
        ],
        "links": [{"source": "seed", "target": "gone", "relation": "calls"}],
    }
    pack = context.build_context(graph, "seed", tree, depth=1, budget=5000)
    bad = next(u for u in pack["unresolved"] if u["id"] == "gone")
    # The old behaviour called this a doc/rationale node. It is a missing file.
    assert bad["reason_code"] == symbols.FILE_UNREADABLE
    assert "no definition at that line" not in bad["reason"]


def test_unsliceable_seed_is_flagged_not_reported_as_an_empty_pack(tree):
    graph = {
        "nodes": [{"id": "seed", "label": "A widget.",
                   "source_file": "pkg/mod.py", "source_location": "L10"}],
        "links": [],
    }
    pack = context.build_context(graph, "seed", tree, depth=1, budget=5000)
    assert pack["included"] == []
    assert pack["seed_resolved"] is False
    assert pack["seed_unresolved_reason"] == symbols.NO_DEFINITION_AT_LINE
    assert next(u for u in pack["unresolved"] if u["is_seed"])


def test_resolved_seed_is_flagged_resolved(tree):
    pack = context.build_context(_graph(), "seed", tree, depth=1, budget=5000)
    assert pack["seed_resolved"] is True
    assert pack["seed_unresolved_reason"] is None


# --------------------------------------------------------------------------
# Phase 0.5 — test-edge provenance must survive into the graph
# --------------------------------------------------------------------------

def test_coverage_and_heuristic_test_edges_carry_different_confidence():
    """One `tests` label, two very different claims — they must be separable.

    Coverage-derived means a run observed the test execute the line; heuristic
    means the names matched. An agent that cannot tell them apart may conclude a
    fix is covered by a test that never touches it.
    """
    from graphify_ext import test_link
    cov = test_link.from_coverage({
        "files": {"app/pay.py": {"contexts": {"12": ["tests/test_pay.py::test_charge|run"]}}}
    })
    assert cov["edges"], "fixture should produce a coverage edge"
    assert all(e["confidence"] == "EXTRACTED" for e in cov["edges"])

    data = {
        "nodes": [
            {"id": "prod", "label": "charge()", "source_file": "app/pay.py",
             "source_location": "L12"},
            {"id": "t", "label": "test_charge()", "source_file": "tests/test_pay.py",
             "source_location": "L3"},
        ],
        "links": [],
    }
    heur = test_link.heuristic(data)
    assert heur["edges"], "fixture should produce a heuristic edge"
    assert all(e["confidence"] == "INFERRED" for e in heur["edges"])


def test_inject_preserves_producer_confidence_and_defaults_to_external(tmp_path):
    """apply() used to hardcode EXTERNAL, flattening the distinction above."""
    import json as _json
    from graphify_ext import edge_inject
    graph = {"nodes": [{"id": "a", "label": "a()", "source_file": "m.py",
                        "source_location": "L1"},
                       {"id": "b", "label": "b()", "source_file": "m.py",
                        "source_location": "L2"}],
             "links": []}
    gp = tmp_path / "graph.json"
    gp.write_text(_json.dumps(graph), encoding="utf-8")
    edge_inject.inject(gp, {"edges": [
        {"relation": "tests", "source_ref": {"node": "a"}, "target_ref": {"node": "b"},
         "confidence": "INFERRED", "detail": "heuristic:name-match"},
        {"relation": "reads_config", "source_ref": {"node": "b"},
         "target_ref": {"node": "a"}},                      # declares nothing
    ]})
    out = _json.loads(gp.read_text(encoding="utf-8"))
    by_rel = {e["relation"]: e for e in out["links"]}
    assert by_rel["tests"]["confidence"] == "INFERRED"
    assert by_rel["reads_config"]["confidence"] == "EXTERNAL"


def test_semgrep_numeric_severity_does_not_land_in_the_label_field():
    """`confidence` is a label; a float there collides with EXTRACTED/INFERRED."""
    from graphify_ext import edge_inject
    out = edge_inject.from_semgrep({"results": [{
        "check_id": "r.taint", "path": "a.py", "start": {"line": 5},
        "extra": {"severity": "ERROR", "dataflow_trace": {
            "taint_source": [{"path": "a.py", "start": {"line": 2}}]}},
    }]})
    assert out["edges"]
    for e in out["edges"]:
        assert isinstance(e["confidence"], str)
        assert isinstance(e["confidence_score"], float)


# --------------------------------------------------------------------------
# Phase 1 — ranking under truncation
# --------------------------------------------------------------------------

def test_score_trades_depth_against_relation_rather_than_ordering_them():
    # The defect being fixed: depth was the primary key, so a distant call lost
    # to a near import. The cure must not simply invert that.
    assert context.score_node("calls", 3) > context.score_node("imports", 1)
    # ...and must not let an arbitrarily distant call win either.
    assert context.score_node("calls", 8) < context.score_node("imports", 1)
    # Same relation, nearer is always better.
    assert context.score_node("calls", 1) > context.score_node("calls", 2)


def test_ranking_is_not_alphabetical(tree):
    """The old tie-break decided 3 of 12 real truncation boundaries by label."""
    graph = {
        "nodes": [
            {"id": "seed", "label": ".label()", "source_file": "pkg/mod.py",
             "source_location": "L18"},
            {"id": "zzz_call", "label": "zzz", "source_file": "pkg/mod.py",
             "source_location": "L4"},
            {"id": "aaa_import", "label": "aaa", "source_file": "pkg/mod.py",
             "source_location": "L25"},
        ],
        "links": [
            {"source": "seed", "target": "zzz_call", "relation": "calls"},
            {"source": "seed", "target": "aaa_import", "relation": "imports"},
        ],
    }
    pack = context.build_context(graph, "seed", tree, depth=1, budget=5000)
    order = [i["id"] for i in pack["included"]]
    # `calls` outranks `imports` at equal depth despite sorting later by label.
    assert order.index("zzz_call") < order.index("aaa_import")


def test_omitted_entries_carry_rank_severity(tree):
    graph = {
        "nodes": [
            {"id": "seed", "label": ".label()", "source_file": "pkg/mod.py",
             "source_location": "L18"},
            {"id": "callee", "label": "module_level()", "source_file": "pkg/mod.py",
             "source_location": "L4"},
        ],
        "links": [{"source": "seed", "target": "callee", "relation": "calls"}],
    }
    pack = context.build_context(graph, "seed", tree, depth=1, budget=40)
    assert pack["omitted"], "budget should force an omission"
    o = pack["omitted"][0]
    assert o["severity"] in ("truncated_high_rank", "truncated_low_rank")
    assert isinstance(o["score"], float)
    # A direct callee at depth 1 is not tail material.
    assert o["severity"] == "truncated_high_rank"


def test_ranking_basis_is_disclosed_in_the_result(tree):
    pack = context.build_context(_graph(), "seed", tree, depth=1, budget=5000)
    assert pack["ranking"].startswith("depth, then relation_weight")
    assert pack["decay"] == context.DEFAULT_DECAY
    assert all("score" in i for i in pack["included"])


# --------------------------------------------------------------------------
# disclosure of what the GRAPH does not contain
# --------------------------------------------------------------------------

NESTED_SRC = '''\
def outer(a):
    def inner(b):
        return b + 1
    return inner(a)
'''


def test_nested_function_absent_from_graph_is_disclosed(tmp_path):
    """The graph emits no node for a function nested in a function.

    Measured: 0 of 610 such symbols had a node across 14 checkouts of
    psf/requests. Nothing downstream can mention what the graph has no record
    of, so the pack recovers it from source and says so — otherwise an agent
    editing `outer` is never told `inner` exists.
    """
    (tmp_path / "m.py").write_text(NESTED_SRC, encoding="utf-8")
    graph = {"nodes": [{"id": "outer", "label": "outer()", "source_file": "m.py",
                        "source_location": "L1"}], "links": []}
    pack = context.build_context(graph, "outer", tmp_path, depth=1, budget=5000)
    names = [u["name"] for u in pack["unmodelled"]]
    assert "outer.inner" in names
    gap = next(u for u in pack["unmodelled"] if u["name"] == "outer.inner")
    assert gap["def_line"] == 2
    assert "no node" in gap["reason"]


def test_symbols_the_graph_does_know_are_not_reported_as_gaps(tmp_path):
    """Anti-vacuity: the gap list must not simply mirror every definition."""
    (tmp_path / "m.py").write_text(NESTED_SRC, encoding="utf-8")
    graph = {"nodes": [
        {"id": "outer", "label": "outer()", "source_file": "m.py",
         "source_location": "L1"},
        {"id": "inner", "label": ".inner()", "source_file": "m.py",
         "source_location": "L2"},
    ], "links": []}
    pack = context.build_context(graph, "outer", tmp_path, depth=1, budget=5000)
    assert pack["unmodelled"] == []


def test_gaps_outside_the_emitted_code_are_not_reported(tmp_path):
    """Only gaps inside what the agent was actually shown are its problem."""
    (tmp_path / "m.py").write_text(
        NESTED_SRC + "\n\ndef unrelated():\n    def hidden():\n        pass\n",
        encoding="utf-8")
    graph = {"nodes": [{"id": "outer", "label": "outer()", "source_file": "m.py",
                        "source_location": "L1"}], "links": []}
    pack = context.build_context(graph, "outer", tmp_path, depth=1, budget=5000)
    assert [u["name"] for u in pack["unmodelled"]] == ["outer.inner"]


# --------------------------------------------------------------------------
# JavaScript: functions bound by assignment, not declared
# --------------------------------------------------------------------------

JS_SRC = '''\
var res = module.exports = {};

res.status = function status(code) {
  this.statusCode = code;
  return this;
};

res.send = function (body) {
  return this.end(body);
};

const helper = (x) => x + 1;

function declared(y) {
  return y;
}

class Thing {
  method() { return 1; }
}
'''


def test_js_functions_bound_by_assignment_are_found(tmp_path):
    """The dominant JS idiom is assignment, not declaration.

    Measured on expressjs/express `lib/response.js`: the declaration node types
    alone found 9 symbols against 20 assigned functions that make up the
    module's entire public API. graphify does not model these either, so without
    this they are invisible to the graph *and* to the gap disclosure whose whole
    job is reporting such absences.
    """
    (tmp_path / "r.js").write_text(JS_SRC, encoding="utf-8")
    syms = symbols.definitions_from_source(
        (tmp_path / "r.js").read_bytes(), "r.js")
    names = {s.name for s in syms}
    assert {"res.status", "res.send", "helper", "declared", "Thing"} <= names


def test_js_assigned_function_keeps_its_receiver(tmp_path):
    """`res.send` must not collapse to `send` — the leaf alone is not an identifier."""
    (tmp_path / "r.js").write_text(JS_SRC, encoding="utf-8")
    syms = symbols.definitions_from_source(
        (tmp_path / "r.js").read_bytes(), "r.js")
    assert "res.send" in {s.name for s in syms}
    assert "send" not in {s.name for s in syms}


def test_js_binding_def_line_is_the_binding_not_the_body(tmp_path):
    """A graph keyed on source_location records the line the binding starts on."""
    (tmp_path / "r.js").write_text(JS_SRC, encoding="utf-8")
    syms = {s.name: s for s in symbols.definitions_from_source(
        (tmp_path / "r.js").read_bytes(), "r.js")}
    assert syms["res.status"].def_line == 3      # `res.status = function ...`
    assert syms["res.status"].end == 6           # through the closing brace


def test_js_extent_slices_the_whole_assigned_function(tmp_path):
    (tmp_path / "r.js").write_text(JS_SRC, encoding="utf-8")
    sym = symbols.resolve(tmp_path, "r.js", 8)   # res.send binding line
    assert sym is not None and sym.name == "send"
    assert "return this.end(body)" in sym.source


def test_python_is_unaffected_by_the_js_binding_rule(tmp_path):
    """A Python assignment of a lambda must NOT become a definition."""
    (tmp_path / "m.py").write_text(
        "f = lambda x: x + 1\n\n\ndef real(y):\n    return y\n", encoding="utf-8")
    syms = symbols.definitions_from_source(
        (tmp_path / "m.py").read_bytes(), "m.py")
    assert [s.name for s in syms] == ["real"]
