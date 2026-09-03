"""Post-fix re-verification (Requirement 2, case 8 / step 7).

Workflow, not a graph feature: snapshot the target node's edge fingerprint
BEFORE the fix, re-extract the changed file(s) after, and diff — an
unexpected edge delta (new callee, dropped validation call, new sink
reachability) is surfaced before the PR is finalized.

Community/cluster attributes are excluded from the fingerprint on purpose:
clustering reassignment churns run-to-run and must not read as a structural
change (spec checklist: no noisy false positives from community churn).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from graphify_ext import graphio

SNAPSHOT_NAME = "verify-fix-snapshot.json"

# Edge attributes that constitute structure. Everything else (community,
# weight, layout hints) is noise for this comparison.
_EDGE_FIELDS = ("source", "target", "relation", "confidence", "origin")


def _fingerprint_edges(data: dict, nid: str) -> list[dict]:
    fp = []
    for e in graphio.edges(data):
        if str(e.get("source")) == nid or str(e.get("target")) == nid:
            fp.append({k: e.get(k) for k in _EDGE_FIELDS if e.get(k) is not None})
    return sorted(fp, key=lambda d: json.dumps(d, sort_keys=True))


def snapshot(out_dir: Path, node_queries: list[str]) -> dict:
    """Record the pre-fix edge fingerprints for the named nodes."""
    graph_path = Path(out_dir) / "graph.json"
    data = graphio.load(graph_path)
    entry: dict = {"taken_at": time.time(), "nodes": {}}
    unresolved = []
    for q in node_queries:
        nid = graphio.resolve_node(data, q)
        if nid is None:
            unresolved.append(q)
            continue
        entry["nodes"][nid] = {
            "query": q,
            "edges": _fingerprint_edges(data, nid),
        }
    entry["unresolved"] = unresolved
    graphio.save_atomic(Path(out_dir) / SNAPSHOT_NAME, entry)
    return entry


def check(out_dir: Path) -> dict:
    """Diff current graph.json against the stored snapshot.

    Returns {"nodes": {nid: {"added": [...], "removed": [...], "ok": bool}},
    "clean": bool}. Run 'graphify update <changed files>' (or commit, letting
    the hook rebuild) between snapshot and check.
    """
    out_dir = Path(out_dir)
    snap_path = out_dir / SNAPSHOT_NAME
    if not snap_path.exists():
        raise FileNotFoundError(
            f"no snapshot at {snap_path} — run 'graphify-ext verify-fix snapshot "
            f"--node <X>' before applying the fix")
    snap = json.loads(snap_path.read_text(encoding="utf-8-sig"))
    data = graphio.load(out_dir / "graph.json")

    result: dict = {"nodes": {}, "clean": True}
    for nid, rec in snap.get("nodes", {}).items():
        before = {json.dumps(e, sort_keys=True) for e in rec.get("edges", [])}
        after = {json.dumps(e, sort_keys=True) for e in _fingerprint_edges(data, nid)}
        added = sorted(json.loads(s) for s in (after - before))
        removed = sorted(json.loads(s) for s in (before - after))
        ok = not added and not removed
        if not ok:
            result["clean"] = False
        result["nodes"][nid] = {
            "query": rec.get("query"),
            "added": added,
            "removed": removed,
            "ok": ok,
        }
    return result


def format_check(result: dict) -> str:
    lines = []
    for nid, rec in result["nodes"].items():
        if rec["ok"]:
            lines.append(f"{rec['query'] or nid}: no structural edge change")
            continue
        lines.append(f"{rec['query'] or nid}: EDGE DELTA — review before finalizing")
        for e in rec["added"]:
            lines.append(f"  + {e.get('source')} --{e.get('relation')}--> {e.get('target')}")
        for e in rec["removed"]:
            lines.append(f"  - {e.get('source')} --{e.get('relation')}--> {e.get('target')}")
    lines.append("clean" if result["clean"] else
                 "NOT CLEAN: unexpected deltas above may be intended (removed vulnerable "
                 "call) or regressions (dropped validation, new sink) — verify each")
    return "\n".join(lines)
