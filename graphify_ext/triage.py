"""End-to-end vuln triage pipeline (Requirement 2, step 6).

Input: an identify-layer report — a JSON list of vulns, each at minimum
``{"id", "description", "file", "line"}`` (optional ``"function"`` overrides
location-based resolution). Output: one agent-ready context object per vuln,
covering the nine cases:

  1 direct callers/callees      -> ``neighbors``
  2 transitive blast radius     -> ``blast_radius`` (scoped subgraph)
  3 overrides                   -> ``overrides``
  4 taint-exposed subset        -> ``taint_exposed`` (needs injected edges)
  5 test coverage               -> ``has_test_coverage`` / ``covering_tests``
  6 config dependencies         -> ``config_dependencies`` (needs injected edges)
  7 duplicate patterns          -> out-of-band by design (``notes`` says how)
  8 post-fix verification       -> separate ``verify_fix`` workflow step
  9 cross-repo                  -> ``notes`` points at ``graphify global``

The scoped subgraph — not the whole graph.json — is what goes into the
agent's context; ``--depth``/``--max-nodes`` keep it token-bounded.
"""
from __future__ import annotations

import json
from pathlib import Path

from graphify_ext import blast_radius as br
from graphify_ext import graphio


def resolve_target(data: dict, vuln: dict,
                   root: "Path | None" = None) -> str | None:
    """Resolve a vuln to a graph node.

    ``root`` is forwarded to the location lookup so its containment guards
    can read the source file. Without it those guards silently do nothing
    and a module-level finding is attributed to the function above it —
    the exact mis-attribution they exist to prevent, in the entry point an
    agent actually calls.
    """
    if vuln.get("function"):
        nid = graphio.resolve_node(data, str(vuln["function"]))
        if nid is not None:
            return nid
    if vuln.get("file") and vuln.get("line") is not None:
        nid = graphio.resolve_by_location(data, str(vuln["file"]),
                                          int(vuln["line"]), root=root)
        if nid is not None:
            return nid
    if vuln.get("file"):
        return graphio.resolve_node(data, str(vuln["file"]))
    return None


def _neighbors(data: dict, nid: str,
               relations: "tuple[str, ...] | None" = None) -> dict:
    """Direct callers/callees of ``nid`` (case 1).

    ``relations`` MUST be the same set the blast radius was traversed with.
    This function used to read ``br.DEFAULT_RELATIONS`` directly, independently
    of what ``blast_radius`` actually followed — so a narrowed traversal
    produced a context whose ``neighbors`` listed a caller that appeared
    nowhere in ``blast_radius.nodes``. An agent reading that gets a
    self-contradictory picture, which is worse than a narrower one.
    """
    relation_set = set(relations if relations is not None else br.DEFAULT_RELATIONS)
    callers, callees = [], []
    idx = graphio.node_index(data)
    for e in graphio.edges(data):
        rel = str(e.get("relation", ""))
        if rel not in relation_set:
            continue
        if str(e.get("target")) == nid and str(e.get("source")) in idx:
            callers.append({"id": str(e.get("source")),
                            "label": idx[str(e.get("source"))].get("label"),
                            "relation": rel})
        elif str(e.get("source")) == nid and str(e.get("target")) in idx:
            callees.append({"id": str(e.get("target")),
                            "label": idx[str(e.get("target"))].get("label"),
                            "relation": rel})
    return {"callers": callers, "callees": callees}


def _coverage(data: dict, nid: str) -> dict:
    idx = graphio.node_index(data)
    covering = [
        {"id": str(e.get("source")),
         "label": idx.get(str(e.get("source")), {}).get("label"),
         "detail": e.get("detail")}
        for e in graphio.edges(data)
        if str(e.get("relation")) == "tests" and str(e.get("target")) == nid
    ]
    return {"has_test_coverage": bool(covering), "covering_tests": covering}


def _config_deps(data: dict, nid: str) -> list[dict]:
    idx = graphio.node_index(data)
    return [
        {"id": str(e.get("target")),
         "label": idx.get(str(e.get("target")), {}).get("label"),
         "source_file": idx.get(str(e.get("target")), {}).get("source_file"),
         "detail": e.get("detail")}
        for e in graphio.edges(data)
        if str(e.get("relation")) == "reads_config" and str(e.get("source")) == nid
    ]


def triage_one(data: dict, vuln: dict, *, depth: int = 2, max_nodes: int = 300,
               root: "Path | None" = None,
               relations: "tuple[str, ...] | None" = None) -> dict:
    target = resolve_target(data, vuln, root=root)
    if target is None:
        return {
            "vuln": vuln,
            "resolved": False,
            "error": "could not resolve vuln location to a graph node "
                     "(is the graph current? does file:line exist?)",
        }
    rels = relations if relations is not None else br.DEFAULT_RELATIONS
    radius = br.blast_radius(data, target, depth=depth, max_nodes=max_nodes,
                             relations=rels)
    idx = graphio.node_index(data)
    ctx = {
        "vuln": vuln,
        "resolved": True,
        "target": {
            "id": target,
            "label": idx[target].get("label"),
            "source_file": idx[target].get("source_file"),
            "source_location": idx[target].get("source_location"),
        },
        "neighbors": _neighbors(data, target, relations=rels),
        "blast_radius": radius,
        "taint_exposed": br.taint_exposed(radius),
        "overrides": br.overrides_of(data, target),
        **_coverage(data, target),
        "config_dependencies": _config_deps(data, target),
        "notes": {
            "duplicate_patterns": "out-of-band: run a pattern search (e.g. semgrep) "
                                  "seeded by this vuln's signature; not a graph problem",
            "cross_repo": "if this code is a shared library, query the global graph "
                          "(graphify global path / merge-graphs) — repos must be "
                          "pre-registered via 'graphify global add'",
            "post_fix": "after editing, run 'graphify-ext verify-fix check' to diff "
                        "this node's edges against the pre-fix snapshot",
        },
    }
    if not ctx["taint_exposed"]["edges"]:
        ctx["notes"]["taint"] = ("no injected taint edges in radius — either the fix "
                                 "site is not reachable from untrusted input, or no "
                                 "taint findings were injected (run 'graphify-ext "
                                 "inject --semgrep <out.json>' first)")
    return ctx


def triage_report(graph_path: Path, report: list[dict], *,
                  depth: int = 2, max_nodes: int = 300,
                  relations: "tuple[str, ...] | None" = None) -> list[dict]:
    data = graphio.load(graph_path)
    # Derived without following symlinks — see graphio.repo_root_for.
    root = graphio.repo_root_for(graph_path)
    return [triage_one(data, v, depth=depth, max_nodes=max_nodes, root=root,
                       relations=relations)
            for v in report]


def summarize(contexts: list[dict]) -> str:
    lines = []
    for c in contexts:
        v = c["vuln"]
        vid = v.get("id", "?")
        if not c.get("resolved"):
            lines.append(f"- {vid}: UNRESOLVED - {c.get('error')}")
            continue
        r = c["blast_radius"]
        taint = len(c["taint_exposed"]["nodes"])
        cov = "covered" if c["has_test_coverage"] else "NO TEST COVERAGE"
        n_over = len(c["overrides"])
        lines.append(
            f"- {vid}: {c['target']['label']} "
            f"[radius: {r['node_count']} nodes/{r['edge_count']} edges"
            f"{', TRUNCATED' if r['truncated'] else ''}; "
            f"taint-exposed: {taint}; {cov}; overrides: {n_over}; "
            f"config deps: {len(c['config_dependencies'])}]"
        )
    return "\n".join(lines)
