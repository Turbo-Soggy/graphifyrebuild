"""External-edge injection into graph.json (Requirement 2, cases 4/5/6).

Architecture decision (spec step 2, decided up front): MERGE into graph.json.
The agent then queries ONE graph; blast-radius, triage, and stock ``graphify
query`` all see the injected edges with no join layer. The cost — rebuilds
rewrite graph.json and drop injected edges — is paid once here:

* The findings themselves are persisted to ``graphify-out/external-findings.json``
  (which lives in the branch slot, so findings are per-branch and survive
  swaps).
* ``reapply()`` re-resolves and re-injects them; the customized post-commit /
  post-checkout hook bodies call it after every rebuild, so the merged graph
  is self-healing.
* Injected edges carry ``origin: "graphify-ext"`` so they are removable
  idempotently and never mistaken for extractor output. Removal keys on
  ``origin`` alone.
* ``confidence`` is a LABEL, and it is the producer's to set: coverage-measured
  ``tests`` edges are EXTRACTED, name-matched ones INFERRED, semgrep-derived
  ones EXTERNAL (with the numeric severity in ``confidence_score``).
  ``EXTERNAL`` is only the default for a producer that declares nothing.
  Previously this was hardcoded to EXTERNAL for every edge, which erased the
  measured-vs-guessed distinction the agent most needs on a ``tests`` edge.

Findings format (the neutral interchange all producers emit):

    {"edges": [
        {"relation": "taints" | "reaches_sink" | "tests" | "reads_config",
         "source_ref": {"node": "<id-or-label>"} | {"file": "p", "line": N},
         "target_ref": {...},
         "detail": "free text provenance", ...}
    ]}
"""
from __future__ import annotations

import json
from pathlib import Path

from graphify_ext import EXT_ORIGIN, graphio

FINDINGS_NAME = "external-findings.json"

VALID_RELATIONS = ("taints", "reaches_sink", "tests", "reads_config")


def resolve_ref(data: dict, ref: dict, root: "Path | None" = None) -> str | None:
    """Resolve a findings ref to a node id.

    ``{"node": ...}`` uses the same resolution ladder as the CLI seed lookup;
    ``{"file": ..., "line": ...}`` uses nearest-preceding-callable containment;
    ``{"file": ...}`` alone resolves to the file-level node.

    ``root`` lets the line lookup reject a location past the end of the file
    instead of silently attaching to the last definition in it. A line-bearing
    ref that fails to resolve is NOT downgraded to a file-level match — a
    finding pointing at a line that does not exist is bad input and must be
    reported, not quietly turned into a coarser edge.
    """
    if "node" in ref:
        return graphio.resolve_node(data, str(ref["node"]))
    file = ref.get("file")
    if not file:
        return None
    line = ref.get("line")
    if line is not None:
        return graphio.resolve_by_location(data, str(file), int(line), root=root)
    return graphio.resolve_node(data, str(file))


def _strip_ext_edges(data: dict) -> int:
    key = graphio.edges_key(data)
    before = len(data[key])
    data[key] = [e for e in data[key] if e.get("origin") != EXT_ORIGIN]
    return before - len(data[key])


def inject(graph_path: Path, findings: dict) -> dict:
    """Merge findings into graph.json (idempotent: prior ext edges replaced).

    Returns a report: {"applied": N, "removed_previous": N, "unresolved": [...]}.
    """
    data = graphio.load(graph_path)
    removed = _strip_ext_edges(data)
    key = graphio.edges_key(data)
    # Never Path.resolve() here — it follows the per-branch cache's
    # graphify-out symlink and yields .graphify-cache as the root, silently
    # disabling every guard in resolve_by_location. See graphio.repo_root_for.
    root = graphio.repo_root_for(graph_path)

    applied = 0
    unresolved: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for f in findings.get("edges", []):
        relation = str(f.get("relation", ""))
        if relation not in VALID_RELATIONS:
            unresolved.append({**f, "reason": f"unknown relation {relation!r}"})
            continue
        src = resolve_ref(data, f.get("source_ref", {}), root=root)
        tgt = resolve_ref(data, f.get("target_ref", {}), root=root)
        if src is None or tgt is None:
            unresolved.append({
                **f,
                "reason": "source_ref unresolved" if src is None else "target_ref unresolved",
            })
            continue
        dedupe = (src, tgt, relation)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        # Honour a producer's declared confidence; EXTERNAL only as the default
        # for producers that state none. Hardcoding EXTERNAL here erased a real
        # distinction: test_link emits both coverage-measured (EXTRACTED) and
        # name-matched (INFERRED) `tests` edges, and from_semgrep carries its
        # own score -- all of which arrived in the graph looking identical, so
        # an agent could not tell a measured fact from a guess. Removal keys on
        # `origin` (see strip above), never on confidence, so varying it here
        # keeps injection idempotent.
        edge = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": f.get("confidence") or "EXTERNAL",
            "origin": EXT_ORIGIN,
        }
        if f.get("confidence_score") is not None:
            edge["confidence_score"] = f["confidence_score"]
        if f.get("detail"):
            edge["detail"] = str(f["detail"])
        data[key].append(edge)
        applied += 1

    graphio.save_atomic(graph_path, data)
    return {"applied": applied, "removed_previous": removed, "unresolved": unresolved}


def store_findings(out_dir: Path, findings: dict, *, merge: bool = True) -> Path:
    """Persist findings as the slot-local source of truth for reapply().

    ``merge=True`` unions with previously stored findings by (relation,
    source_ref, target_ref) so taint, test, and config producers can each
    contribute without clobbering one another.
    """
    path = Path(out_dir) / FINDINGS_NAME
    stored = {"edges": []}
    if merge and path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            stored = {"edges": []}

    def _key(e: dict):
        return (
            str(e.get("relation")),
            json.dumps(e.get("source_ref", {}), sort_keys=True),
            json.dumps(e.get("target_ref", {}), sort_keys=True),
        )

    merged = {_key(e): e for e in stored.get("edges", [])}
    for e in findings.get("edges", []):
        merged[_key(e)] = e
    payload = {"edges": list(merged.values())}
    graphio.save_atomic(path, payload)
    return path


def reapply(out_dir: Path) -> int:
    """Re-inject stored findings after a rebuild rewrote graph.json.

    Returns the number of edges applied (0 when there is nothing stored).
    Called best-effort from the hook bodies.
    """
    out_dir = Path(out_dir)
    findings_path = out_dir / FINDINGS_NAME
    graph_path = out_dir / "graph.json"
    if not findings_path.exists() or not graph_path.exists():
        return 0
    findings = json.loads(findings_path.read_text(encoding="utf-8-sig"))
    report = inject(graph_path, findings)
    return report["applied"]


# ------------------------------------------------------------ producer adapters

def from_semgrep(semgrep_json: dict, *,
                 taint_rules: "list[str] | None" = None,
                 assume_taint: bool = False) -> dict:
    """Map Semgrep JSON to findings edges (case 4 producer).

    Returns ``{"edges": [...], "skipped": [...]}`` — nothing is dropped
    silently in either direction.

    Three cases, because real semgrep output forced the distinction:

    1. **Finding WITH a ``dataflow_trace``** — a genuine multi-step flow. Emits
       ``taints`` and ``reaches_sink`` edges from the traced source to the sink.
    2. **Finding WITHOUT a trace, from a rule the caller declares as taint**
       (``taint_rules`` substring match, or ``assume_taint=True``) — semgrep
       omits the trace when source and sink are the same expression, so there
       is no path to describe. Measured against a real scan of the connected
       repo: **9 of 9 taint findings had no trace**, so requiring one dropped
       every finding silently. The source is genuinely unknown, so no ``taints``
       edge is invented; a self ``reaches_sink`` edge marks the node as
       sink-reaching, which is what ``taint_exposed()`` needs.
    3. **Anything else** — recorded in ``skipped``, not turned into an edge.

    Case 3 exists because semgrep's JSON carries **no indication of whether a
    rule ran in taint mode** (verified on real output: ``metadata`` empty,
    ``engine_kind`` only, no mode field). Treating every trace-less finding as
    taint would label ordinary pattern matches as taint-exposed — a confidently
    wrong security claim. Pattern matches without flow are duplicate-search
    material (case 7), which stays out-of-band by design.
    """
    rules = [r.lower() for r in (taint_rules or [])]
    edges: list[dict] = []
    skipped: list[dict] = []
    for res in semgrep_json.get("results", []):
        trace = (res.get("extra") or {}).get("dataflow_trace")
        sink_file = res.get("path")
        sink_line = (res.get("start") or {}).get("line")
        check = str(res.get("check_id", ""))
        sev = str(((res.get("extra") or {}).get("severity")) or "")
        conf = {"ERROR": 1.0, "WARNING": 0.8, "INFO": 0.5}.get(sev, 0.9)

        if trace:
            src_loc = _semgrep_loc(trace.get("taint_source"))
            if src_loc and sink_file and sink_line:
                # `confidence` is a LABEL in graphify's schema (EXTRACTED/INFERRED/
                # AMBIGUOUS); the numeric severity belongs in `confidence_score`.
                # Emitting a float under `confidence` would collide with the
                # string labels test_link emits for the same key.
                common = {"detail": f"semgrep:{check}",
                          "confidence": "EXTERNAL",
                          "confidence_score": conf}
                for rel in ("taints", "reaches_sink"):
                    edges.append({
                        "relation": rel,
                        "source_ref": {"file": src_loc[0], "line": src_loc[1]},
                        "target_ref": {"file": sink_file, "line": sink_line},
                        **common})
            else:
                skipped.append({"check_id": check, "path": sink_file,
                                "line": sink_line,
                                "reason": "trace present but source location unreadable"})
            continue

        is_taint = assume_taint or any(r in check.lower() for r in rules)
        if is_taint and sink_file and sink_line:
            edges.append({
                "relation": "reaches_sink",
                "source_ref": {"file": sink_file, "line": sink_line},
                "target_ref": {"file": sink_file, "line": sink_line},
                "detail": f"semgrep:{check} (sink location only; no dataflow trace)",
                "confidence": "EXTERNAL",
                "confidence_score": conf * 0.9,
            })
        else:
            skipped.append({
                "check_id": check, "path": sink_file, "line": sink_line,
                "reason": ("no dataflow trace and rule not declared as taint "
                           "(pass --taint-rule/--assume-taint if it is)"),
            })
    return {"edges": edges, "skipped": skipped}


def _semgrep_loc(obj) -> tuple[str, int] | None:
    """Extract (path, line) from semgrep's assorted trace-location shapes."""
    if obj is None:
        return None
    # CliLoc form: [ {"path":..., "start": {"line":...}}, "text" ]
    if isinstance(obj, list) and obj:
        obj = obj[0]
    if isinstance(obj, dict):
        path = obj.get("path")
        start = obj.get("start") or {}
        line = start.get("line") if isinstance(start, dict) else None
        if path and line:
            return str(path), int(line)
        # Nested "location" wrapper
        loc = obj.get("location")
        if isinstance(loc, dict):
            return _semgrep_loc(loc)
    return None
