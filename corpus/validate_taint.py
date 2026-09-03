#!/usr/bin/env python3
"""Validate taint-edge MAPPING against the known-ground-truth corpus (critique #2).

Scope, stated precisely because it is easy to overclaim: neither build detects
taint. Both map an external analyzer's findings onto graph nodes. What is
validated here is the mapping and the exposed-subset filter — not the
analyzer's accuracy. See corpus/README.md.

Checks, per build:
  M1  each flow's SOURCE end resolves to the expected enclosing function node
  M2  each flow's SINK end resolves to the expected enclosing function node
  M3  a cross-function flow resolves to two DIFFERENT nodes (not one lucky match)
  M4  the taint-exposed set equals ground truth exactly
  M5  no true-negative function appears in the exposed set
  M6  findings that cannot be resolved are REPORTED, not silently dropped
  M7  an out-of-range line does not silently produce a wrong edge
  M8  containment boundaries: def lines, in-body lines, module-level
      statements and past-EOF all resolve to the right node (or to none)

Usage: python validate_taint.py [--build a|b|both] [--keep]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GT = json.loads((HERE / "ground_truth.json").read_text(encoding="utf-8"))
PY = sys.executable
SCRIPTS = Path(PY).parent

RESULTS: list[tuple[str, str, bool, str]] = []


def check(build: str, cid: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((build, cid, ok, name + (f" — {detail}" if detail else "")))
    print(f"  {'PASS' if ok else 'FAIL'}  [{build}/{cid}] {name}"
          + (f" — {detail}" if detail else ""))
    return ok


def info(build: str, cid: str, name: str, detail: str) -> None:
    print(f"  INFO  [{build}/{cid}] {name} — {detail}")


# ------------------------------------------------------------------ workspace

def make_workspace() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="taint-corpus-"))
    shutil.copytree(HERE / "vuln_app", ws / "vuln_app")
    subprocess.run(["git", "init", "-q", "-b", "main", str(ws)],
                   check=True, capture_output=True)
    for cfg in (("user.email", "c@example.com"), ("user.name", "c"),
                ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(ws), "config", *cfg],
                       check=True, capture_output=True)
    (ws / ".gitignore").write_text("graphify-out/\n.code-review-graph/\n",
                                   encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-q", "-m", "corpus"],
                   check=True, capture_output=True)
    return ws


HANDLERS_REL = "vuln_app/handlers.py"


def call_lines(ws: Path) -> dict[tuple[str, str], int]:
    """{(enclosing_function, called_name): line} resolved from the AST.

    Line numbers are derived, never hardcoded, so editing the corpus cannot
    silently invalidate the expectations.
    """
    src = (ws / HANDLERS_REL).read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: dict[tuple[str, str], int] = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name:
                    out.setdefault((fn.name, name), node.lineno)
    return out


def flow_locations(ws: Path) -> list[dict]:
    lines = call_lines(ws)
    flows = []
    for f in GT["flows"]:
        s = (f["source"]["function"], f["source"]["call"])
        k = (f["sink"]["function"], f["sink"]["call"])
        if s not in lines or k not in lines:
            raise RuntimeError(f"{f['id']}: could not locate {s} / {k} in the AST")
        flows.append({**f, "source_line": lines[s], "sink_line": lines[k]})
    return flows


BOUNDARY_REL = "vuln_app/boundaries.py"


def boundary_positions(ws: Path) -> list[dict]:
    """Resolve each declarative boundary case to a concrete line, via the AST.

    Never hardcodes line numbers: editing boundaries.py shifts every position
    automatically instead of silently invalidating the expectations.
    """
    src = (ws / BOUNDARY_REL).read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assigns: dict[str, int] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name):
                    assigns[tgt.id] = n.lineno

    out: list[dict] = []
    for case in GT["boundary_cases"]:
        at = case["at"]
        kind = at["kind"]
        if kind == "def_line":
            line = funcs[at["function"]].lineno
        elif kind == "in_body":
            fn = funcs[at["function"]]
            line = fn.lineno + 1
            if fn.end_lineno and fn.end_lineno > fn.lineno:
                line = fn.end_lineno          # last line of the body
        elif kind == "module_level":
            line = assigns[at["assignment"]]
        elif kind == "past_eof":
            line = len(src.splitlines()) + 5000
        else:
            raise RuntimeError(f"{case['id']}: unknown position kind {kind!r}")
        out.append({**case, "line": line})
    return out


def classify(resolved: "str | None", label_of) -> tuple[str, "str | None"]:
    """Normalise a build's answer to ('function'|'file'|'none', name).

    Both builds are compared on the resolved NAME, never on node ids — the two
    builds use different id schemes by design, so an id comparison would be
    testing the schemes rather than the resolution.
    """
    if resolved is None:
        return ("none", None)
    label = label_of(resolved)
    if label is None:
        return ("none", None)
    if label.endswith("()"):
        return ("function", label[:-2])
    if label.endswith(".py") or "/" in label or "\\" in label:
        return ("file", label.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    return ("function", label)


def check_boundaries(build: str, ws: Path, resolve, label_of) -> None:
    """Run the M8 boundary suite for one build.

    ``resolve(rel_path, line)`` returns that build's node identifier or None;
    ``label_of(identifier)`` returns a printable label.
    """
    for case in boundary_positions(ws):
        got_kind, got_name = classify(resolve(BOUNDARY_REL, case["line"]), label_of)
        want = case["expect"]
        if want["kind"] == "function":
            ok = got_kind == "function" and got_name == want["name"]
            wanted = f"function {want['name']}"
        elif want["kind"] == "file":
            ok = got_kind == "file"
            wanted = "the FILE node"
        else:
            ok = got_kind == "none"
            wanted = "unresolved"
        check(build, f"M8 {case['id']}", case["why"], ok,
              f"L{case['line']} -> {got_kind}"
              + (f" {got_name}" if got_name else "") + f"; want {wanted}")


def last_line(ws: Path) -> int:
    return len((ws / HANDLERS_REL).read_text(encoding="utf-8").splitlines())


# -------------------------------------------------------------------- build A

def run_build_a(ws: Path) -> None:
    print("\n=== Build A (graphify_ext) ===")
    sys.path.insert(0, str(ROOT))
    from graphify_ext import edge_inject, graphio

    env = dict(os.environ, PYTHONHASHSEED="0", GRAPHIFY_MAX_WORKERS="1")
    r = subprocess.run([str(SCRIPTS / "graphify.exe"), ".", "--code-only"],
                       cwd=str(ws), capture_output=True, text=True, env=env)
    graph_path = ws / "graphify-out" / "graph.json"
    if not graph_path.exists():
        check("A", "build", "corpus graph built", False,
              (r.stdout + r.stderr).strip()[-200:])
        return
    check("A", "build", "corpus graph built", True)

    flows = flow_locations(ws)
    findings = {"edges": []}
    for f in flows:
        for rel in ("taints", "reaches_sink"):
            findings["edges"].append({
                "relation": rel,
                "source_ref": {"file": HANDLERS_REL, "line": f["source_line"]},
                "target_ref": {"file": HANDLERS_REL, "line": f["sink_line"]},
                "detail": f"{f['id']}:{f['kind']}",
            })
    for probe in GT["unresolvable_probes"]:
        findings["edges"].append({
            "relation": "taints",
            "source_ref": {"file": probe["file"].replace("corpus/", ""),
                           "line": probe["line"]},
            "target_ref": {"file": HANDLERS_REL, "line": flows[0]["sink_line"]},
            "detail": probe["id"],
        })

    report = edge_inject.inject(graph_path, findings)
    data = graphio.load(graph_path)
    idx = graphio.node_index(data)

    def label_of(nid: str) -> str:
        return str(idx.get(nid, {}).get("label", nid)).rstrip("()")

    ext = [e for e in graphio.edges(data) if e.get("origin") == "graphify-ext"]
    by_detail: dict[str, list[dict]] = {}
    for e in ext:
        by_detail.setdefault(str(e.get("detail", "")).split(":")[0], []).append(e)

    for f in flows:
        edges = by_detail.get(f["id"], [])
        if not edges:
            check("A", f"M1/M2 {f['id']}", "flow mapped to graph nodes", False,
                  "no edge produced")
            continue
        src_ok = all(label_of(str(e["source"])) == f["expected_source_node"] for e in edges)
        snk_ok = all(label_of(str(e["target"])) == f["expected_sink_node"] for e in edges)
        check("A", f"M1 {f['id']}", "source end resolves to expected node", src_ok,
              f"got {label_of(str(edges[0]['source']))}, want {f['expected_source_node']}")
        check("A", f"M2 {f['id']}", "sink end resolves to expected node", snk_ok,
              f"got {label_of(str(edges[0]['target']))}, want {f['expected_sink_node']}")
        if f["expected_source_node"] != f["expected_sink_node"]:
            check("A", f"M3 {f['id']}", "cross-function flow maps to two distinct nodes",
                  edges[0]["source"] != edges[0]["target"])

    exposed = {label_of(str(e["source"])) for e in ext} | \
              {label_of(str(e["target"])) for e in ext}
    exposed -= {label_of(str(e["source"])) for e in ext
                if str(e.get("detail", "")).startswith("UR")}
    expected = set(GT["expected_exposed_nodes"])
    real_exposed = {x for x in exposed if x in expected or x in
                    {n["function"] for n in GT["negatives"]}}
    check("A", "M4", "taint-exposed set matches ground truth", real_exposed == expected,
          f"extra={sorted(real_exposed - expected)} missing={sorted(expected - real_exposed)}")
    negatives = {n["function"] for n in GT["negatives"]}
    check("A", "M5", "no true-negative function is exposed",
          not (exposed & negatives), f"leaked={sorted(exposed & negatives)}")

    unresolved_ids = {str(u.get("detail", "")) for u in report["unresolved"]}
    check("A", "M6", "missing-file finding reported, not dropped",
          "UR1" in unresolved_ids, f"unresolved={sorted(unresolved_ids)}")
    ur2_edges = by_detail.get("UR2", [])
    ur2_resolved_to = label_of(str(ur2_edges[0]["source"])) if ur2_edges else None
    check("A", "M7", "out-of-range line does not silently produce a wrong edge",
          "UR2" in unresolved_ids,
          f"line {GT['unresolvable_probes'][1]['line']} (file has {last_line(ws)}) "
          f"resolved to '{ur2_resolved_to}'" if ur2_resolved_to else "correctly unresolved")

    # M8 — containment boundaries. Build A has no function extents, so these
    # rely on graphio's guards; the repo root must be passed or they go inert.
    check_boundaries(
        "A", ws,
        resolve=lambda rel, line: graphio.resolve_by_location(data, rel, line, root=ws),
        label_of=lambda nid: str(idx.get(nid, {}).get("label", nid)) if nid else None,
    )


# -------------------------------------------------------------------- build B

def run_build_b(ws: Path) -> None:
    print("\n=== Build B (crg) ===")
    sys.path.insert(0, str(ROOT / "crg"))
    import taint_inject

    r = subprocess.run([str(SCRIPTS / "code-review-graph.exe"), "build", "--quiet"],
                       cwd=str(ws), capture_output=True, text=True)
    db = ws / ".code-review-graph" / "graph.db"
    if not db.exists():
        check("B", "build", "corpus graph built", False,
              (r.stdout + r.stderr).strip()[-200:])
        return
    check("B", "build", "corpus graph built", True)

    flows = flow_locations(ws)
    findings = {"edges": []}
    for f in flows:
        for kind in ("TAINTS", "REACHES_SINK"):
            findings["edges"].append({
                "kind": kind,
                "source": {"file": HANDLERS_REL, "line": f["source_line"]},
                "sink": {"file": HANDLERS_REL, "line": f["sink_line"]},
                "detail": f"{f['id']}:{f['kind']}",
            })
    for probe in GT["unresolvable_probes"]:
        findings["edges"].append({
            "kind": "TAINTS",
            "source": {"file": probe["file"].replace("corpus/", ""), "line": probe["line"]},
            "sink": {"file": HANDLERS_REL, "line": flows[0]["sink_line"]},
            "detail": probe["id"],
        })

    report = taint_inject.apply_findings(ws, findings, store=False)
    rows = taint_inject.taint_rows(ws)

    def fn_of(q: str) -> str:
        return q.split("::")[-1]

    by_detail: dict[str, list[dict]] = {}
    for row in rows:
        by_detail.setdefault(str(row["detail"]).split(":")[0], []).append(row)

    for f in flows:
        edges = by_detail.get(f["id"], [])
        if not edges:
            check("B", f"M1/M2 {f['id']}", "flow mapped to graph nodes", False,
                  "no edge produced")
            continue
        src_ok = all(fn_of(e["source_qualified"]) == f["expected_source_node"] for e in edges)
        snk_ok = all(fn_of(e["target_qualified"]) == f["expected_sink_node"] for e in edges)
        check("B", f"M1 {f['id']}", "source end resolves to expected node", src_ok,
              f"got {fn_of(edges[0]['source_qualified'])}, want {f['expected_source_node']}")
        check("B", f"M2 {f['id']}", "sink end resolves to expected node", snk_ok,
              f"got {fn_of(edges[0]['target_qualified'])}, want {f['expected_sink_node']}")
        if f["expected_source_node"] != f["expected_sink_node"]:
            check("B", f"M3 {f['id']}", "cross-function flow maps to two distinct nodes",
                  edges[0]["source_qualified"] != edges[0]["target_qualified"])

    real = [r for r in rows if not str(r["detail"]).startswith("UR")]
    exposed = {fn_of(r["source_qualified"]) for r in real} | \
              {fn_of(r["target_qualified"]) for r in real}
    expected = set(GT["expected_exposed_nodes"])
    check("B", "M4", "taint-exposed set matches ground truth", exposed == expected,
          f"extra={sorted(exposed - expected)} missing={sorted(expected - exposed)}")
    negatives = {n["function"] for n in GT["negatives"]}
    check("B", "M5", "no true-negative function is exposed",
          not (exposed & negatives), f"leaked={sorted(exposed & negatives)}")

    unresolved_ids = {str(u.get("detail", "")) for u in report["unresolved"]}
    check("B", "M6", "missing-file finding reported, not dropped",
          "UR1" in unresolved_ids, f"unresolved={sorted(unresolved_ids)}")
    ur2 = by_detail.get("UR2", [])
    check("B", "M7", "out-of-range line does not silently produce a wrong edge",
          "UR2" in unresolved_ids,
          f"resolved to '{fn_of(ur2[0]['source_qualified'])}'" if ur2 else "correctly unresolved")

    # M8 — same boundaries against Build B, which HAS real line_start/line_end
    # extents. It should pass by construction; if it ever fails, the extents
    # are not being used the way this suite assumes.
    conn = taint_inject.connect(ws, readonly=True)
    try:
        check_boundaries(
            "B", ws,
            resolve=lambda rel, line: taint_inject.resolve_location(conn, ws, rel, line),
            label_of=lambda q: q.split("::")[-1] + "()" if q and "::" in q else q,
        )
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", choices=["a", "b", "both"], default="both")
    ap.add_argument("--keep", action="store_true", help="keep the workspace")
    args = ap.parse_args()

    ws = make_workspace()
    print(f"workspace: {ws}")
    try:
        if args.build in ("a", "both"):
            run_build_a(ws)
        if args.build in ("b", "both"):
            run_build_b(ws)
    finally:
        if not args.keep:
            shutil.rmtree(ws, ignore_errors=True)

    print("\n== summary ==")
    failed = [r for r in RESULTS if not r[2]]
    for build, cid, ok, name in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  [{build}/{cid}] {name}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
