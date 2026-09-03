"""Materialise definitions the extractor has no node for.

The measured problem this solves
--------------------------------
On the 70-task Phase 0 corpus, **17 tasks could not be scored at all** because
the symbol the fix was *about* had no graph node: 15 of 30 on expressjs/express
and 2 of 20 on pallets/flask. Two upstream causes, both verified in source:

* **Assignment-bound members are not modelled.** Express binds its entire
  public API as ``res.json = function json(obj) {...}``. graphify materialises
  ``this.x = fn`` / ``exports.x = fn`` / ``Foo.prototype.x = fn``, but an
  arbitrary identifier receiver only when it is a direct object-literal binding
  in the same scope (``_js_member_assignment_target``, the #1077 guard). Ten
  nodes for ``lib/response.js``, none of them the twenty methods.
* **Id collisions drop the second definition.** ``@typing.overload`` stubs of
  ``stream_with_context`` and the real body slug to the same id, so the real
  one vanishes (upstream #3302; the same defect loses
  ``HTTPAdapter.get_connection`` behind ``_get_connection`` in requests).

A graph that has no record of a symbol cannot be queried about it. Disclosure
(``context.unmodelled``) tells the agent *that* something is missing; this pass
makes the missing thing *queryable*: a real node with the extractor's own
schema, a ``contains``/``method`` edge from its owner, and conservative
``calls`` edges recovered from the source.

What it deliberately does NOT do
--------------------------------
* It never touches a node graphify emitted. Existing ids, labels and edges are
  left byte-identical; ``graphify query`` sees a superset, never a rewrite.
* It does not materialise functions nested inside functions. graphify omits
  those by design across every language, the pack already discloses them, and
  a node for every closure would flood containment for no retrieval gain.
* Its ``calls`` edges are **INFERRED**, never EXTRACTED: a callee is linked
  only when its leaf name resolves to exactly one callable, in the same file
  first and only then across the graph. An ambiguous name emits nothing.
  Every supplement edge carries ``origin: "graphify-ext:supplement"`` so
  provenance is a field, not a guess, and re-application is idempotent.
* It is inert until asked for. ``graphify-ext supplement`` writes a marker into
  the output slot; the hook bodies re-apply after every rebuild only when the
  marker exists, so a repo that never opted in keeps a graph identical to
  stock's (the Section A differential tests depend on that).
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from graphify_ext import graphio, symbols

SUPPLEMENT_ORIGIN = "graphify-ext:supplement"
MARKER_NAME = "supplement.json"

# Reasons a definition had no node. Stable strings: agents may branch on them.
REASON_MEMBER = "assignment_bound_member"      # res.json = function ...
REASON_COLLISION = "id_collision_or_overload"  # another node has this leaf name
REASON_ABSENT = "absent_from_extraction"       # nothing else explains it


def _normalize_id(s: str) -> str:
    """Port of upstream ``graphify.ids.normalize_id`` so ids match the
    extractor's own convention without importing graphify (this module must
    work on a bare graph.json)."""
    cur = s
    for _ in range(6):
        nxt = unicodedata.normalize("NFKC", cur.casefold())
        if nxt == cur:
            break
        cur = nxt
    cur = re.sub(r"[^\w]+", "_", cur, flags=re.UNICODE)
    cur = re.sub(r"_+", "_", cur)
    return cur.strip("_")


def make_id(*parts: str) -> str:
    return _normalize_id("_".join(p.strip("_.") for p in parts if p))


def _leaf(label: str) -> str:
    return str(label or "").strip().lstrip(".").rstrip("()")


def _line(node: dict) -> int | None:
    loc = str(node.get("source_location") or "")
    return int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None


def _is_callable(node: dict) -> bool:
    return bool(node.get("_callable")) or str(node.get("label", "")).endswith("()")


def _file_nodes(data: dict) -> dict[str, dict]:
    """source_file -> the FILE node (label is the basename, sits at L1)."""
    out: dict[str, dict] = {}
    for n in graphio.nodes(data):
        if graphio.is_file_node(n):
            out.setdefault(str(n["source_file"]), n)
    return out


def _file_is_stale(f: str, defs: list, known_callable_lines: set[int],
                   manifest: "dict | None", root: Path) -> str | None:
    """Why ``f`` must NOT be supplemented, or None if it is safe to.

    A stale file is the one input that makes this pass harmful instead of
    inert: after an edit shifts lines, every extractor node in the file points
    at a line where no definition begins, so every definition looks "missing"
    and would be materialised a second time under a fresh id. Two guards:

    1. the manifest hash, when a manifest is available -- the same check the
       context pack uses to report ``stale_files``;
    2. without one, a structural tell: a callable node the graph has for this
       file whose ``source_location`` is a line where the parser found no
       definition at all. A fresh extraction never produces that.
    """
    if manifest is not None:
        entry = manifest.get(f)
        recorded = str((entry or {}).get("ast_hash") or (entry or {}).get("hash") or "")
        if recorded:
            import hashlib
            try:
                h = hashlib.md5(usedforsecurity=False)
                h.update((root / f).read_bytes())
            except OSError:
                return "file unreadable"
            if h.hexdigest() != recorded:
                return "edited since extraction (manifest hash differs)"
    def_lines = {d.def_line for d in defs}
    orphan = sorted(ln for ln in known_callable_lines if ln not in def_lines)
    if orphan:
        return (f"graph node(s) at line(s) {orphan[:5]} where no definition "
                f"begins -- the graph predates an edit to this file")
    return None


def compute(data: dict, root: Path, files: "list[str] | None" = None,
            manifest: "dict | None" = None) -> dict:
    """Nodes and edges to add. Pure: reads the graph and the source, writes nothing.

    Returns ``{"nodes", "edges", "skipped", "stale_files", "stats"}``.
    ``skipped`` lists definitions seen in source but deliberately not
    materialised, with the reason, so "the supplement found nothing" and "the
    supplement declined" stay distinguishable. ``stale_files`` lists files
    refused whole because the graph no longer matches them (see
    :func:`_file_is_stale`); re-extract and re-run.
    """
    root = Path(root)
    file_nodes = _file_nodes(data)
    targets = files if files is not None else sorted(file_nodes)
    callable_lines: dict[str, set[int]] = {}
    for n in graphio.nodes(data):
        if n.get("source_file") and _is_callable(n) and _line(n) is not None:
            callable_lines.setdefault(str(n["source_file"]), set()).add(_line(n))

    ids = set(graphio.node_index(data))
    # (file, def_line, leaf) -> node id, for "does the graph already know this"
    known: dict[tuple[str, int, str], str] = {}
    leaves_in_file: dict[str, set[str]] = {}
    for n in graphio.nodes(data):
        f, ln = n.get("source_file"), _line(n)
        if not f or ln is None:
            continue
        leaf = _leaf(n.get("label"))
        known[(str(f), ln, leaf)] = str(n["id"])
        # `qualified_name` is only ever set by this module; the extractor's
        # nodes are keyed on their label leaf alone.
        if n.get("qualified_name"):
            known[(str(f), ln, str(n["qualified_name"]).split(".")[-1])] = str(n["id"])
        if _is_callable(n):
            leaves_in_file.setdefault(str(f), set()).add(leaf)

    new_nodes: list[dict] = []
    new_edges: list[dict] = []
    skipped: list[dict] = []
    stale_files: list[dict] = []
    stats = {"files_scanned": 0, "files_unreadable": 0, "files_unsupported": 0,
             "files_stale": 0, "definitions_seen": 0, "already_modelled": 0}

    # Per file: every definition with the node id it maps to (existing or
    # new), kept for the call-resolution pass below.
    per_file_defs: dict[str, list[tuple[symbols.Symbol, str]]] = {}
    new_ids: set[str] = set()

    for f in targets:
        fnode = file_nodes.get(f)
        if fnode is None:
            continue
        if symbols.language_for(f) is None:
            stats["files_unsupported"] += 1
            continue
        defs = symbols.definitions_in(root, f)
        if defs is None:
            stats["files_unreadable"] += 1
            continue
        why_stale = _file_is_stale(f, defs, callable_lines.get(f, set()), manifest, root)
        if why_stale is not None:
            stats["files_stale"] += 1
            stale_files.append({"file": f, "reason": why_stale})
            continue
        stats["files_scanned"] += 1
        kinds = {s.name: s.kind for s in defs}
        extents = {s.name: (s.start, s.end) for s in defs}
        by_name: dict[str, str] = {}
        rows: list[tuple[symbols.Symbol, str]] = []
        file_id = str(fnode["id"])

        for sym in defs:
            stats["definitions_seen"] += 1
            leaf = sym.name.split(".")[-1]
            existing = known.get((f, sym.def_line, leaf))
            if existing is not None:
                stats["already_modelled"] += 1
                by_name[sym.name] = existing
                rows.append((sym, existing))
                continue
            if symbols.is_nested_in_function(sym.name, kinds, sym.def_line, extents):
                skipped.append({"name": sym.name, "file": f, "def_line": sym.def_line,
                                "reason": "nested inside a function; graphify "
                                          "omits these by design and the pack "
                                          "discloses them as unmodelled"})
                continue

            owner_name = sym.name.rsplit(".", 1)[0] if "." in sym.name else None
            owner_kind = kinds.get(owner_name or "")
            owner_id = by_name.get(owner_name or "")
            is_method = owner_kind == "class" and owner_id is not None

            if leaf in leaves_in_file.get(f, set()):
                reason = REASON_COLLISION
            elif "." in sym.name and owner_kind is None:
                reason = REASON_MEMBER
            else:
                reason = REASON_ABSENT

            if sym.kind == "function":
                label = f".{leaf}()" if is_method else f"{leaf}()"
            else:
                label = leaf
            base = make_id(file_id, *sym.name.split("."))
            nid = base
            n = 0
            while nid in ids:
                # Same slug already taken (this is exactly how the extractor
                # lost the original): disambiguate by line, never overwrite.
                n += 1
                nid = f"{base}_l{sym.def_line}" if n == 1 else f"{base}_l{sym.def_line}_{n}"
            ids.add(nid)
            new_ids.add(nid)
            node = {
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": f,
                "source_location": f"L{sym.def_line}",
                "_origin": SUPPLEMENT_ORIGIN,
                "origin": SUPPLEMENT_ORIGIN,
                "qualified_name": sym.name,
                "kind": sym.kind,
                "supplement_reason": reason,
            }
            if sym.kind == "function":
                node["_callable"] = True
            new_nodes.append(node)
            if is_method:
                new_edges.append(_edge(owner_id, nid, "method", "EXTRACTED",
                                       f"supplement:{reason}", f, sym.def_line))
            else:
                new_edges.append(_edge(file_id, nid, "contains", "EXTRACTED",
                                       f"supplement:{reason}", f, sym.def_line))
            by_name[sym.name] = nid
            rows.append((sym, nid))
            if sym.kind == "function":
                leaves_in_file.setdefault(f, set()).add(leaf)
        per_file_defs[f] = rows

    if not new_nodes:
        return {"nodes": [], "edges": [], "skipped": skipped,
                "stale_files": stale_files, "stats": stats}

    # ---- calls: conservative, INFERRED, only where a new node is an endpoint
    # Callable index over existing + new nodes, per file and globally.
    global_idx: dict[str, list[str]] = {}
    file_idx: dict[str, dict[str, list[str]]] = {}
    for n in list(graphio.nodes(data)) + new_nodes:
        if not _is_callable(n) or not n.get("source_file"):
            continue
        leaf = _leaf(n.get("label"))
        global_idx.setdefault(leaf, []).append(str(n["id"]))
        file_idx.setdefault(str(n["source_file"]), {}).setdefault(leaf, []).append(str(n["id"]))

    seen: set[tuple[str, str, str]] = set()
    for f, rows in per_file_defs.items():
        local = file_idx.get(f, {})
        for sym, sid in rows:
            if sym.kind != "function":
                continue
            own_leaf = sym.name.split(".")[-1]
            for callee in sym.calls:
                if callee == own_leaf:
                    continue
                cands = local.get(callee, [])
                scope = "same-file"
                if len(cands) != 1:
                    cands = global_idx.get(callee, [])
                    scope = "graph-unique"
                if len(cands) != 1:
                    continue          # ambiguous or unknown: say nothing
                tid = cands[0]
                if tid == sid or (sid not in new_ids and tid not in new_ids):
                    continue          # existing<->existing is the extractor's job
                key = (sid, tid, "calls")
                if key in seen:
                    continue
                seen.add(key)
                new_edges.append(_edge(sid, tid, "calls", "INFERRED",
                                       f"supplement:name-match({scope})",
                                       f, sym.def_line))

    return {"nodes": new_nodes, "edges": new_edges, "skipped": skipped,
            "stale_files": stale_files, "stats": stats}


def _edge(src: str, tgt: str, relation: str, confidence: str, detail: str,
          file: str, line: int) -> dict:
    return {
        "source": src, "target": tgt, "relation": relation,
        "confidence": confidence, "origin": SUPPLEMENT_ORIGIN,
        "_origin": SUPPLEMENT_ORIGIN, "detail": detail,
        "source_file": file, "source_location": f"L{line}",
    }


def strip(data: dict) -> tuple[int, int]:
    """Remove everything this module previously added. Returns (nodes, edges)."""
    nodes = graphio.nodes(data)
    gone = {str(n["id"]) for n in nodes if n.get("origin") == SUPPLEMENT_ORIGIN}
    before_n = len(nodes)
    data["nodes"] = [n for n in nodes if n.get("origin") != SUPPLEMENT_ORIGIN]
    key = graphio.edges_key(data)
    before_e = len(data[key])
    data[key] = [e for e in data[key]
                 if e.get("origin") != SUPPLEMENT_ORIGIN
                 and str(e.get("source")) not in gone
                 and str(e.get("target")) not in gone]
    return before_n - len(data["nodes"]), before_e - len(data[key])


def apply(graph_path: Path, root: "Path | None" = None,
          files: "list[str] | None" = None) -> dict:
    """Strip any previous supplement, recompute from source, merge, save.

    Idempotent: running it twice yields the same graph. ``root`` defaults to
    the repo root derived WITHOUT following symlinks (see
    :func:`graphio.repo_root_for`) so a linked per-branch slot still reads the
    working tree, not the cache directory.
    """
    graph_path = Path(graph_path)
    data = graphio.load(graph_path)
    removed_n, removed_e = strip(data)
    root = Path(root) if root is not None else graphio.repo_root_for(graph_path)
    manifest = None
    mp = graph_path.parent / "manifest.json"
    if mp.exists():
        try:
            m = graphio.read_json(mp)
            manifest = m if isinstance(m, dict) else None
        except Exception:
            manifest = None
    result = compute(data, root, files=files, manifest=manifest)
    data["nodes"].extend(result["nodes"])
    data[graphio.edges_key(data)].extend(result["edges"])
    graphio.save_atomic(graph_path, data)
    return {
        "added_nodes": len(result["nodes"]),
        "added_edges": len(result["edges"]),
        "removed_previous_nodes": removed_n,
        "removed_previous_edges": removed_e,
        "skipped": result["skipped"],
        "stale_files": result["stale_files"],
        "stats": result["stats"],
        "by_reason": _count(result["nodes"], "supplement_reason"),
        "edges_by_relation": _count(result["edges"], "relation"),
    }


def _count(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key))
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def enable(out_dir: Path) -> Path:
    """Record that this slot wants the supplement re-applied after rebuilds."""
    path = Path(out_dir) / MARKER_NAME
    graphio.save_atomic(path, {"enabled": True, "origin": SUPPLEMENT_ORIGIN})
    return path


def is_enabled(out_dir: Path) -> bool:
    return (Path(out_dir) / MARKER_NAME).exists()


def reapply(out_dir: Path) -> dict | None:
    """Hook entry point: re-run after a rebuild, only if this slot opted in."""
    out_dir = Path(out_dir)
    graph_path = out_dir / "graph.json"
    if not is_enabled(out_dir) or not graph_path.exists():
        return None
    return apply(graph_path)
