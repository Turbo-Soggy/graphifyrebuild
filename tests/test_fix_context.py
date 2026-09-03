import json
from pathlib import Path

import pytest

from graphify_ext import (blast_radius as br, config_link, edge_inject,
                          graphio, test_link, triage, verify_fix)


class TestResolve:
    def test_resolve_by_id(self, toy_graph):
        assert graphio.resolve_node(toy_graph, "app.validate") == "app.validate"

    def test_resolve_by_label(self, toy_graph):
        assert graphio.resolve_node(toy_graph, "validate()") == "app.validate"

    def test_resolve_bare_name(self, toy_graph):
        assert graphio.resolve_node(toy_graph, "validate") == "app.validate"

    def test_ambiguous_returns_none(self, toy_graph):
        # "check" matches Base.check and Sub.check
        assert graphio.resolve_node(toy_graph, "check") is None

    def test_resolve_by_location_nearest_preceding(self, toy_graph):
        assert graphio.resolve_by_location(toy_graph, "src/app.py", 35) == "app.validate"
        assert graphio.resolve_by_location(toy_graph, "src/app.py", 12) == "app.handler"

    def test_resolve_by_location_no_match(self, toy_graph):
        assert graphio.resolve_by_location(toy_graph, "src/app.py", 5) is None
        assert graphio.resolve_by_location(toy_graph, "nope.py", 100) is None

    def test_prefers_enclosing_callable_over_later_docstring_node(self, toy_graph):
        """Regression: graphify emits a non-callable docstring node one line
        BELOW each function's own node. Picking the nearest preceding node of
        any kind resolved every in-function location to the docstring, so
        taint/coverage findings attached to prose instead of code."""
        toy_graph["nodes"].append({
            "id": "a.validate.doc",
            "label": "Validate the payload before use.",
            "source_file": "src/app.py",
            "source_location": "L31",
            "file_type": "code",
        })
        # Line 35 sits inside validate() (L30); the docstring node at L31 is
        # nearer, but is not something a finding can meaningfully attach to.
        assert graphio.resolve_by_location(toy_graph, "src/app.py", 35) == "app.validate"

    def test_falls_back_to_non_callable_when_no_callable_precedes(self, toy_graph):
        toy_graph["nodes"].append({
            "id": "hdr.note", "label": "module header",
            "source_file": "src/hdr.py", "source_location": "L2",
            "file_type": "code",
        })
        assert graphio.resolve_by_location(toy_graph, "src/hdr.py", 9) == "hdr.note"

    @staticmethod
    def _realistic_source(tmp_path):
        """A file shaped like real code: defs at column 0 (matching the toy
        graph's L10/L30/L50 nodes), bodies indented, one module-level statement.

        The earlier version of this fixture wrote 60 lines of ``line {i}`` —
        every one at column 0, i.e. every one at module scope. That is not what
        source looks like, and it made the test assert that a module-scope line
        resolves to a function, which is the exact mis-attribution the guards
        exist to prevent.
        """
        src = tmp_path / "src"
        src.mkdir()
        lines = ["    pass" for _ in range(60)]
        lines[9] = "def handler():"           # L10
        lines[29] = "def validate():"         # L30
        lines[44] = "MODULE_LEVEL_CONST = 1"  # L45, module scope after validate
        lines[49] = "def sanitize():"         # L50
        (src / "app.py").write_text("\n".join(lines), encoding="utf-8")
        return tmp_path

    def test_repo_root_does_not_follow_the_output_symlink(self, tmp_path):
        """Regression: the per-branch cache replaces graphify-out with a link to
        .graphify-cache/<branch>/. Deriving the repo root with Path.resolve()
        followed that link and returned .graphify-cache, under which no source
        file exists — so every file-reading guard in resolve_by_location went
        silently inert in exactly the deployment they were added for. Measured
        on the connected repo."""
        repo = tmp_path / "repo"
        (repo / ".graphify-cache" / "main").mkdir(parents=True)
        (repo / ".graphify-cache" / "main" / "graph.json").write_text(
            '{"nodes": []}', encoding="utf-8")
        out = repo / "graphify-out"
        # Reuse the product's own linker: plain symlink_to() fails under this
        # machine's user-profile temp (no Developer Mode), which would skip the
        # test on the very platform the bug was found on. _make_link falls back
        # to a Windows directory junction, which resolve() follows identically.
        from graphify_ext.branch_cache import _make_link
        if not _make_link(out, repo / ".graphify-cache" / "main"):
            pytest.skip("filesystem supports neither symlinks nor junctions")

        graph_path = out / "graph.json"
        assert graphio.repo_root_for(graph_path) == repo
        # The bug this replaced: resolve() lands in .graphify-cache instead.
        assert Path(graph_path).resolve().parent.parent != repo

    def test_indented_body_resolves_to_enclosing_function(self, toy_graph, tmp_path):
        root = self._realistic_source(tmp_path)
        assert graphio.resolve_by_location(
            toy_graph, "src/app.py", 35, root=root) == "app.validate"

    def test_definition_line_resolves_to_its_own_node(self, toy_graph, tmp_path):
        """The `def` line is itself at column 0, so the top-level guard must not
        swallow it — a finding ON a definition belongs to that definition."""
        root = self._realistic_source(tmp_path)
        assert graphio.resolve_by_location(
            toy_graph, "src/app.py", 30, root=root) == "app.validate"

    def test_module_scope_line_is_not_blamed_on_preceding_function(
            self, toy_graph, tmp_path):
        """Regression (measured 2/4 mis-attributed before the guard): a
        module-level statement — a hardcoded secret, a taint source in config
        code — must not be attributed to whichever function happens to sit
        above it. The toy graph has no file-level node, so the honest answer is
        None: 'cannot attribute' beats 'blame the function above'."""
        root = self._realistic_source(tmp_path)
        assert graphio.resolve_by_location(
            toy_graph, "src/app.py", 45, root=root) is None
        # Contrast, so this test cannot pass vacuously: without a root the
        # guard cannot run, and the bare nearest-preceding-callable heuristic
        # produces exactly the mis-attribution described above.
        assert graphio.resolve_by_location(
            toy_graph, "src/app.py", 45) == "app.validate"

    def test_line_past_end_of_file_is_unresolved(self, toy_graph, tmp_path):
        """Regression: a line beyond the file's end resolved to the last
        definition, turning a bogus finding into a plausible-looking edge."""
        root = self._realistic_source(tmp_path)
        assert graphio.resolve_by_location(
            toy_graph, "src/app.py", 99999, root=root) is None
        # Without a root there is nothing to bound against, so the heuristic
        # still answers — callers that can supply a root should.
        assert graphio.resolve_by_location(toy_graph, "src/app.py", 99999) is not None


class TestBlastRadius:
    def test_up_finds_transitive_callers(self, toy_graph):
        r = br.blast_radius(toy_graph, "app.sanitize", depth=2, direction="up")
        ids = {n["id"] for n in r["nodes"]}
        assert "app.validate" in ids       # 1 hop
        assert "app.handler" in ids        # 2 hops
        depths = {n["id"]: n["blast_depth"] for n in r["nodes"]}
        assert depths["app.handler"] == 2

    def test_depth_cap(self, toy_graph):
        r = br.blast_radius(toy_graph, "app.sanitize", depth=1, direction="up")
        ids = {n["id"] for n in r["nodes"]}
        assert "app.validate" in ids
        assert "app.handler" not in ids

    def test_down_finds_callees(self, toy_graph):
        r = br.blast_radius(toy_graph, "app.handler", depth=2, direction="down")
        ids = {n["id"] for n in r["nodes"]}
        assert {"app.validate", "app.sanitize"} <= ids

    def test_max_nodes_truncation(self, toy_graph):
        r = br.blast_radius(toy_graph, "app.sanitize", depth=5,
                            direction="up", max_nodes=2)
        assert r["truncated"]
        assert r["node_count"] <= 2

    def test_class_seed_walks_member_methods(self, toy_graph):
        # Callers bind to Base.check (the method node); seeding from the CLASS
        # must still find them via the member-seed hop.
        r = br.blast_radius(toy_graph, "base.Base", depth=1, direction="up")
        ids = {n["id"] for n in r["nodes"]}
        assert "app.handler" in ids
        assert "sub.Sub" in ids  # inherits edge

    def test_subgraph_closure_includes_injected_edges(self, toy_graph):
        toy_graph["edges"].append({
            "source": "app.handler", "target": "app.sanitize",
            "relation": "taints", "confidence": "EXTERNAL", "origin": "graphify-ext",
        })
        r = br.blast_radius(toy_graph, "app.sanitize", depth=2, direction="up")
        relations = {e["relation"] for e in r["edges"]}
        assert "taints" in relations
        exposed = br.taint_exposed(r)
        assert {n["id"] for n in exposed["nodes"]} == {"app.handler", "app.sanitize"}

    def test_narrowed_relations_returns_a_strict_subset(self, toy_graph):
        """The `relations` parameter existed but was passed by no caller in the
        repo — untested code despite existing. Narrowing must actually narrow."""
        # Seed the CLASS: sub.Sub reaches base.Base via `inherits`, so dropping
        # that relation must drop that node. (base.Base.check has no inbound
        # inherits edge, so it could not demonstrate narrowing at all.)
        wide = br.blast_radius(toy_graph, "base.Base", depth=2, direction="up")
        narrow = br.blast_radius(toy_graph, "base.Base", depth=2,
                                 direction="up", relations=("calls",))
        wide_ids = {n["id"] for n in wide["nodes"]}
        narrow_ids = {n["id"] for n in narrow["nodes"]}
        assert narrow_ids < wide_ids, "narrowing to 'calls' did not drop any node"
        # `inherits` is what reaches sub.Sub in the toy graph, so dropping it
        # must drop that node specifically.
        assert "sub.Sub" in wide_ids and "sub.Sub" not in narrow_ids

    def test_containment_is_opt_in_not_default(self, toy_graph):
        """`contains`/`method` are the most common relations in a real graph;
        following them by default floods every radius. They must be reachable
        on request and absent otherwise."""
        assert "contains" not in br.DEFAULT_RELATIONS
        assert "method" not in br.DEFAULT_RELATIONS
        members = br.blast_radius(toy_graph, "base.Base", depth=1, direction="down",
                                  relations=br.MEMBER_RELATIONS)
        assert "base.Base.check" in {n["id"] for n in members["nodes"]}

    def test_closure_still_reports_edges_outside_the_traversal_set(self, toy_graph):
        """Policy: `relations` bounds which edges are FOLLOWED, not which are
        REPORTED. A narrowed walk must still surface injected taint edges
        between the nodes it selected — otherwise narrowing silently discards
        the security context it was narrowed to find."""
        toy_graph["edges"].append({
            "source": "app.handler", "target": "app.validate",
            "relation": "taints", "confidence": "EXTERNAL", "origin": "graphify-ext",
        })
        r = br.blast_radius(toy_graph, "app.validate", depth=1, direction="up",
                            relations=("calls",))
        assert "taints" in {e["relation"] for e in r["edges"]}
        assert br.taint_exposed(r)["edges"], "taint edges lost under a narrowed walk"

    def test_unknown_seed_raises(self, toy_graph):
        with pytest.raises(KeyError):
            br.blast_radius(toy_graph, "nope")


class TestOverrides:
    def test_override_found(self, toy_graph):
        out = br.overrides_of(toy_graph, "base.Base.check")
        assert len(out) == 1
        assert out[0]["id"] == "sub.Sub.check"
        assert out[0]["owning_class"] == "sub.Sub"

    def test_no_overrides_for_plain_function(self, toy_graph):
        assert br.overrides_of(toy_graph, "app.validate") == []


class TestInject:
    def _write(self, tmp_path, toy_graph):
        p = tmp_path / "graph.json"
        p.write_text(json.dumps(toy_graph), encoding="utf-8")
        return p

    def test_inject_and_idempotent_reapply(self, tmp_path, toy_graph):
        p = self._write(tmp_path, toy_graph)
        findings = {"edges": [{
            "relation": "taints",
            "source_ref": {"node": "handler"},
            "target_ref": {"file": "src/app.py", "line": 55},
        }]}
        r1 = edge_inject.inject(p, findings)
        assert r1["applied"] == 1 and not r1["unresolved"]
        r2 = edge_inject.inject(p, findings)
        assert r2["applied"] == 1 and r2["removed_previous"] == 1
        data = graphio.load(p)
        ext = [e for e in graphio.edges(data) if e.get("origin") == "graphify-ext"]
        assert len(ext) == 1
        assert ext[0]["source"] == "app.handler"
        assert ext[0]["target"] == "app.sanitize"  # L55 -> nearest preceding def (L50)
        assert ext[0]["confidence"] == "EXTERNAL"

    def test_out_of_range_line_reported_not_downgraded(self, tmp_path, toy_graph):
        """A line-bearing ref that cannot resolve must be REPORTED, not quietly
        downgraded to a coarser file-level edge."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        out = tmp_path / "graphify-out"
        out.mkdir()
        p = out / "graph.json"
        p.write_text(json.dumps(toy_graph), encoding="utf-8")
        r = edge_inject.inject(p, {"edges": [{
            "relation": "taints",
            "source_ref": {"file": "src/app.py", "line": 99999},
            "target_ref": {"node": "handler"},
        }]})
        assert r["applied"] == 0
        assert len(r["unresolved"]) == 1

    def test_unresolved_reported_not_applied(self, tmp_path, toy_graph):
        p = self._write(tmp_path, toy_graph)
        r = edge_inject.inject(p, {"edges": [{
            "relation": "taints",
            "source_ref": {"node": "missing_fn"},
            "target_ref": {"node": "handler"},
        }]})
        assert r["applied"] == 0
        assert len(r["unresolved"]) == 1

    def test_unknown_relation_rejected(self, tmp_path, toy_graph):
        p = self._write(tmp_path, toy_graph)
        r = edge_inject.inject(p, {"edges": [{
            "relation": "banana",
            "source_ref": {"node": "handler"},
            "target_ref": {"node": "validate"},
        }]})
        assert r["applied"] == 0 and r["unresolved"]

    def test_store_findings_merges_producers(self, tmp_path):
        e1 = {"edges": [{"relation": "taints", "source_ref": {"node": "a"},
                         "target_ref": {"node": "b"}}]}
        e2 = {"edges": [{"relation": "tests", "source_ref": {"node": "t"},
                         "target_ref": {"node": "a"}}]}
        edge_inject.store_findings(tmp_path, e1)
        edge_inject.store_findings(tmp_path, e2)
        stored = json.loads((tmp_path / edge_inject.FINDINGS_NAME).read_text())
        assert len(stored["edges"]) == 2

    def test_reapply_after_rebuild(self, tmp_path, toy_graph):
        p = self._write(tmp_path, toy_graph)
        findings = {"edges": [{
            "relation": "taints",
            "source_ref": {"node": "handler"},
            "target_ref": {"node": "sanitize"},
        }]}
        edge_inject.store_findings(tmp_path, findings)
        edge_inject.inject(p, findings)
        # Simulate a rebuild rewriting graph.json without ext edges.
        p.write_text(json.dumps(toy_graph), encoding="utf-8")
        assert edge_inject.reapply(tmp_path) == 1

    def test_from_semgrep_taint_trace(self):
        semgrep = {"results": [{
            "check_id": "python.sqli",
            "path": "src/app.py",
            "start": {"line": 51},
            "extra": {"dataflow_trace": {
                "taint_source": [{"path": "src/app.py", "start": {"line": 11}}],
            }},
        }]}
        f = edge_inject.from_semgrep(semgrep)
        rels = sorted(e["relation"] for e in f["edges"])
        assert rels == ["reaches_sink", "taints"]

    def test_traceless_taint_finding_is_not_silently_dropped(self):
        """Regression from at-scale validation: real semgrep taint findings whose
        source and sink are the same expression carry NO dataflow_trace — 9 of 9
        on the connected repo. Requiring a trace dropped every one of them
        silently."""
        raw = {"results": [{
            "check_id": "rules.ts-input-to-fs", "path": "src/cli.ts",
            "start": {"line": 121}, "extra": {"severity": "WARNING"},
        }]}
        # Not declared as taint -> reported as skipped, never guessed at.
        f = edge_inject.from_semgrep(raw)
        assert f["edges"] == []
        assert len(f["skipped"]) == 1
        assert "not declared as taint" in f["skipped"][0]["reason"]
        # Declared as taint -> mapped, marking the node as sink-reaching.
        f = edge_inject.from_semgrep(raw, taint_rules=["ts-input-to-fs"])
        assert len(f["edges"]) == 1 and f["skipped"] == []
        assert f["edges"][0]["relation"] == "reaches_sink"
        assert "no dataflow trace" in f["edges"][0]["detail"]

    def test_traceless_findings_are_never_assumed_to_be_taint(self):
        """Semgrep's JSON carries no indication of whether a rule ran in taint
        mode (verified on real output: empty metadata, no mode field). Treating
        every trace-less finding as taint would label ordinary pattern matches
        as taint-exposed — a confidently wrong security claim."""
        raw = {"results": [{
            "check_id": "rules.uses-md5", "path": "src/a.ts",
            "start": {"line": 10}, "extra": {"severity": "INFO"},
        }]}
        assert edge_inject.from_semgrep(raw)["edges"] == []
        assert edge_inject.from_semgrep(raw, assume_taint=True)["edges"]

    def test_from_semgrep_ignores_non_taint(self):
        f = edge_inject.from_semgrep({"results": [{
            "check_id": "x", "path": "a.py", "start": {"line": 1}, "extra": {}}]})
        assert f["edges"] == []


class TestTestLink:
    def test_heuristic_links_unique_target(self, toy_graph):
        f = test_link.heuristic(toy_graph)
        assert len(f["edges"]) == 1
        e = f["edges"][0]
        assert e["relation"] == "tests"
        assert e["source_ref"] == {"node": "t.test_validate"}
        assert e["target_ref"] == {"node": "app.validate"}

    def test_heuristic_skips_ambiguous(self, toy_graph):
        # Two production nodes named dup() -> test_dup must link to neither.
        toy_graph["nodes"] += [
            {"id": "d1", "label": "dup()", "source_file": "src/d1.py",
             "source_location": "L1"},
            {"id": "d2", "label": "dup()", "source_file": "src/d2.py",
             "source_location": "L1"},
            {"id": "t.dup", "label": "test_dup()", "source_file": "tests/test_d.py",
             "source_location": "L1"},
        ]
        f = test_link.heuristic(toy_graph)
        assert all(e["source_ref"] != {"node": "t.dup"} for e in f["edges"])

    def test_from_coverage_contexts(self):
        cov = {"files": {"src/app.py": {"contexts": {
            "31": ["tests/test_app.py::test_validate|run"],
            "32": [""],  # import-time execution: ignored
        }}}}
        f = test_link.from_coverage(cov)
        assert len(f["edges"]) == 1
        assert f["edges"][0]["source_ref"] == {"node": "test_validate"}
        assert f["edges"][0]["target_ref"] == {"file": "src/app.py", "line": 31}

    def test_coverage_of_test_files_ignored(self):
        cov = {"files": {"tests/test_app.py": {"contexts": {
            "3": ["tests/test_app.py::test_validate|run"]}}}}
        assert test_link.from_coverage(cov)["edges"] == []

    def test_is_test_path(self):
        assert test_link.is_test_path("tests/test_app.py")
        assert test_link.is_test_path("src/foo.spec.ts")
        assert not test_link.is_test_path("src/contest.py")
        assert not test_link.is_test_path("src/latest/x.py")


class TestConfigLink:
    def test_scan_links_env_read_to_definition(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text(
            "import os\n\ndef f():\n    return os.environ['API_KEY']\n",
            encoding="utf-8")
        (tmp_path / ".env.example").write_text("API_KEY=x\nOTHER=y\n", encoding="utf-8")
        f = config_link.scan(tmp_path)
        assert len(f["edges"]) == 1
        e = f["edges"][0]
        assert e["relation"] == "reads_config"
        assert e["source_ref"] == {"file": "src/app.py", "line": 4}
        assert e["target_ref"] == {"file": ".env.example"}
        assert e["detail"] == "env:API_KEY"

    def test_process_env_and_getenv_forms(self, tmp_path):
        (tmp_path / "a.ts").write_text("const k = process.env.API_KEY;\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("import os\nk = os.getenv('API_KEY')\n", encoding="utf-8")
        (tmp_path / ".env").write_text("API_KEY=1\n", encoding="utf-8")
        f = config_link.scan(tmp_path)
        assert len(f["edges"]) == 2

    def test_undefined_var_yields_nothing(self, tmp_path):
        (tmp_path / "a.py").write_text("import os\nk = os.getenv('NOWHERE')\n", encoding="utf-8")
        assert config_link.scan(tmp_path)["edges"] == []


class TestVerifyFix:
    def test_clean_roundtrip(self, graph_dir):
        verify_fix.snapshot(graph_dir, ["validate"])
        result = verify_fix.check(graph_dir)
        assert result["clean"]

    def test_edge_delta_detected(self, graph_dir, toy_graph):
        verify_fix.snapshot(graph_dir, ["validate"])
        # The fix removes the sanitize call and adds a new callee.
        toy_graph["edges"] = [e for e in toy_graph["edges"]
                              if not (e["source"] == "app.validate"
                                      and e["target"] == "app.sanitize")]
        toy_graph["edges"].append({"source": "app.validate", "target": "app.handler",
                                   "relation": "calls", "confidence": "EXTRACTED"})
        (graph_dir / "graph.json").write_text(json.dumps(toy_graph), encoding="utf-8")
        result = verify_fix.check(graph_dir)
        assert not result["clean"]
        rec = result["nodes"]["app.validate"]
        assert len(rec["added"]) == 1 and len(rec["removed"]) == 1

    def test_community_churn_is_not_a_delta(self, graph_dir, toy_graph):
        verify_fix.snapshot(graph_dir, ["validate"])
        for e in toy_graph["edges"]:
            e["community"] = 42  # clustering reassignment noise
        (graph_dir / "graph.json").write_text(json.dumps(toy_graph), encoding="utf-8")
        assert verify_fix.check(graph_dir)["clean"]

    def test_check_without_snapshot_raises(self, graph_dir):
        (graph_dir / verify_fix.SNAPSHOT_NAME).unlink(missing_ok=True)
        with pytest.raises(FileNotFoundError):
            verify_fix.check(graph_dir)


class TestTriage:
    def test_full_context_assembly(self, graph_dir, toy_graph):
        # Add taint + config edges as an injector would.
        findings = {"edges": [
            {"relation": "taints", "source_ref": {"node": "handler"},
             "target_ref": {"node": "sanitize"}},
        ]}
        edge_inject.inject(graph_dir / "graph.json", findings)
        vulns = [{"id": "V1", "description": "XSS", "file": "src/app.py", "line": 52}]
        ctxs = triage.triage_report(graph_dir / "graph.json", vulns)
        assert len(ctxs) == 1
        c = ctxs[0]
        assert c["resolved"]
        assert c["target"]["id"] == "app.sanitize"
        assert {n["id"] for n in c["blast_radius"]["nodes"]} >= {"app.validate", "app.handler"}
        assert c["taint_exposed"]["edges"]
        assert not c["has_test_coverage"]  # no tests edge injected on sanitize
        summary = triage.summarize(ctxs)
        assert "V1" in summary and "taint-exposed" in summary

    def test_coverage_flag_via_tests_edge(self, graph_dir, toy_graph):
        edge_inject.inject(graph_dir / "graph.json", {"edges": [
            {"relation": "tests", "source_ref": {"node": "t.test_validate"},
             "target_ref": {"node": "app.validate"}},
        ]})
        vulns = [{"id": "V2", "description": "x", "function": "validate"}]
        c = triage.triage_report(graph_dir / "graph.json", vulns)[0]
        assert c["has_test_coverage"]
        assert c["covering_tests"][0]["id"] == "t.test_validate"

    def test_neighbors_never_contradicts_the_blast_radius(self, graph_dir):
        """Regression: `_neighbors` read DEFAULT_RELATIONS directly, independent
        of what `blast_radius` traversed, so a narrowed triage could list a
        caller under `neighbors` that appeared nowhere in `blast_radius.nodes`
        — a self-contradictory agent context."""
        for rels in (("calls",), ("inherits",), br.DEFAULT_RELATIONS):
            c = triage.triage_report(graph_dir / "graph.json",
                                     [{"id": "V9", "description": "x",
                                       "function": "validate"}],
                                     relations=rels)[0]
            neigh = c["neighbors"]["callers"] + c["neighbors"]["callees"]

            # The invariant the drift actually threatened: neighbours must be
            # computed with the SAME relation set the radius was traversed with.
            # (Set containment is the wrong test — `neighbors` reports callees
            # too, and the radius walks "up", so callees are legitimately
            # outside it.)
            assert {n["relation"] for n in neigh} <= set(rels), (
                f"relations={rels}: neighbours used relations outside the "
                f"traversal set")

            # Callers, which the upward radius does cover, must appear in it.
            radius_ids = {n["id"] for n in c["blast_radius"]["nodes"]}
            caller_ids = {n["id"] for n in c["neighbors"]["callers"]}
            assert caller_ids <= radius_ids, (
                f"relations={rels}: callers {caller_ids - radius_ids} "
                f"absent from the radius")

    def test_unresolvable_vuln_reported(self, graph_dir):
        vulns = [{"id": "V3", "description": "x", "file": "nope.py", "line": 1}]
        c = triage.triage_report(graph_dir / "graph.json", vulns)[0]
        assert not c["resolved"]
        assert "UNRESOLVED" in triage.summarize([c])

    def test_overrides_surface_in_context(self, graph_dir):
        vulns = [{"id": "V4", "description": "x", "function": "base.Base.check"}]
        c = triage.triage_report(graph_dir / "graph.json", vulns)[0]
        assert [o["id"] for o in c["overrides"]] == ["sub.Sub.check"]
