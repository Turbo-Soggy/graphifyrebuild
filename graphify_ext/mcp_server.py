"""MCP server exposing the graphify-ext fix-context tools (research gap #4).

Build B (crg) already speaks MCP; Build A was CLI-only, so an agent had to
shell out and re-parse stdout. Every reference design that specifies an
agent-facing query surface — LocAgent's tools, CodexGraph's semantic
primitives, crg's own server — assumes the agent calls a tool in-loop.

Structure is copied from ``crg-upstream/code_review_graph/main.py`` rather than
invented, so the two builds behave the same way for a client:

* server object built at module scope, ``FastMCP(name, version=, instructions=)``
  (main.py:90-98)
* tools registered with a bare ``@mcp.tool()`` — no arguments (main.py:101+)
* tools return a **plain dict**; errors are returned as data,
  ``{"status": "error", "error": ...}``, never raised (tools/_common.py:22-26)
* the ``--tools`` allow-list removes via ``mcp.local_provider.remove_tool``,
  not the private ``_tool_manager._tools`` removed in fastmcp>=3
  (main.py:1114-1173)
* stdio runs with ``show_banner=False`` — the banner corrupts the JSON-RPC
  handshake (main.py:1236-1238)

One deliberate divergence: crg sets ``WindowsSelectorEventLoopPolicy`` before
``mcp.run`` (main.py:1221-1231), but its stated reason is pre-warming
sentence-transformers/torch on the main thread so lazy DLL init cannot deadlock
a worker. This server does pure JSON work — no torch, no threads, no
subprocesses — and the call is deprecated as of Python 3.14 (slated for removal
in 3.16). Verified by removing it and re-running the stdio suite: 6/6 still
pass. Copying it would have shipped a deprecated call for a reason that does
not apply here.

**fastmcp is an OPTIONAL dependency.** ``graphify_ext`` is deliberately
dependency-free so its JSON commands run anywhere — including inside git hooks,
under an interpreter that has graphify but not fastmcp. The import therefore
happens inside :func:`build_server`, never at module import, so
``import graphify_ext`` keeps working without it. Install with::

    pip install -e ".[mcp]"
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from graphify_ext import __version__

DEFAULT_OUT = os.environ.get("GRAPHIFY_OUT", "graphify-out")

#: Every tool this server can expose. ``--tools`` filters this set.
TOOL_NAMES = (
    "blast_radius_tool",
    "context_tool",
    "overrides_tool",
    "triage_tool",
    "edge_diff_tool",
    "list_relations_tool",
    "search_tool",
    "supplement_tool",
    "refresh_tool",
)


def _err(message: str, **extra: Any) -> dict:
    """Standard error envelope — copied from crg tools/_common.py:22-26.

    Errors are returned as data rather than raised so a client sees a
    structured result instead of a transport-level failure.
    """
    return {"status": "error", "error": message, "summary": message, **extra}


def _graph_path(graph: Optional[str]) -> Path:
    return Path(graph) if graph else Path(DEFAULT_OUT) / "graph.json"


def _load(graph: Optional[str]):
    from graphify_ext import graphio
    p = _graph_path(graph)
    if not p.exists():
        raise FileNotFoundError(
            f"no graph at {p} — build it first (graphify . / graphify update .)")
    return graphio.load(p), p


def build_server():
    """Construct the FastMCP server. Imports fastmcp lazily (see module docs)."""
    from fastmcp import FastMCP

    from graphify_ext import blast_radius as br
    from graphify_ext import graphio, triage, verify_fix

    mcp = FastMCP(
        "graphify-ext",
        version=__version__,
        instructions=(
            "Fix-context over a graphify knowledge graph for a coding agent. "
            "Workflow: search_tool to find the symbol (seeds also accept "
            "file:line and qualified names); context_tool for its source plus "
            "neighbourhood within a token budget -- read `unresolved`, "
            "`unmodelled`, `omitted` and `stale_files` before trusting the pack "
            "is complete; blast_radius_tool for who is affected; "
            "overrides_tool before editing a base method; edge_diff_tool "
            "snapshot/check around the edit. Run supplement_tool once per "
            "repo if symbols bound by assignment (JS members) or shadowed by "
            "id collisions are missing. Prefer relation-filtered queries — an "
            "unfiltered 2-hop bidirectional radius on a hot node can cost "
            "~15k tokens, the same walk narrowed to 'calls' ~4k."
        ),
    )

    def _resolve(data: dict, node: str, path: Optional[Path] = None) -> str:
        root = graphio.repo_root_for(path) if path is not None else None
        nid = graphio.resolve_node(data, node, root=root)
        if nid is None:
            # Hand back what the name COULD have meant. An agent that gets a
            # bare "no match" has nothing to retry with; one that sees twenty
            # `send` candidates with files and lines picks the right one.
            cands = graphio.candidates(data, node, limit=15)
            if not cands:
                raise LookupError(f"nothing in the graph matches {node!r}; try "
                                  "search_tool with a shorter name, or "
                                  "supplement_tool if the symbol is bound by "
                                  "assignment or lost to an id collision")
            raise LookupError(
                f"{node!r} is ambiguous ({len(cands)} candidates); pass an id, a "
                "qualified name or file:line -- "
                + "; ".join(f"{c['id']} [{c['label']}] {c['file']}:{c['location']}"
                            for c in cands))
        return nid

    @mcp.tool()
    def blast_radius_tool(
        node: str,
        depth: int = 2,
        direction: str = "up",
        max_nodes: int = 500,
        relations: Optional[list[str]] = None,
        include_containment: bool = False,
        graph: Optional[str] = None,
    ) -> dict:
        """Scoped subgraph around a node — who is affected by changing it.

        Args:
            node: Node id, label, bare name, or source path.
            depth: Hops to traverse (default 2).
            direction: "up" (callers, default), "down" (callees), or "both".
            max_nodes: Cap on nodes; the result reports `truncated`.
            relations: Edge relations to follow. Omit for the structural
                default set. Narrowing is the effective way to cut token cost.
            include_containment: Also follow contains/method edges, answering
                "what is in this class/file". Off by default because
                containment is typically the most common relation in a graph.
            graph: Path to graph.json. Defaults to graphify-out/graph.json.
        """
        try:
            data, path = _load(graph)
            nid = _resolve(data, node, path)
            rels = tuple(relations) if relations else br.DEFAULT_RELATIONS
            if include_containment:
                rels = tuple(dict.fromkeys(rels + br.MEMBER_RELATIONS))
            result = br.blast_radius(data, nid, depth=depth, direction=direction,
                                     max_nodes=max_nodes, relations=rels)
            return {"status": "ok",
                    "summary": (f"{result['node_count']} nodes, "
                                f"{result['edge_count']} edges, "
                                f"~{result['estimated_tokens']} tokens"
                                + (" (TRUNCATED)" if result["truncated"] else "")),
                    **result}
        except Exception as exc:
            return _err(str(exc))

    @mcp.tool()
    def context_tool(
        node: str,
        depth: int = 2,
        direction: str = "both",
        budget: int = 6000,
        per_symbol_cap: int = 80,
        include_containment: bool = True,
        relations: Optional[list[str]] = None,
        index_budget: int = 300,
        graph: Optional[str] = None,
    ) -> dict:
        """Fix-ready context: the symbol's SOURCE plus its neighbourhood.

        Prefer this over blast_radius when the goal is to *change* code rather
        than to survey it: blast_radius returns names and line numbers that must
        then be read from disk, while this returns the actual bodies with exact
        extents, signatures and decorators.

        Args:
            node: Node id, label, bare name, or source path.
            depth: Hops to traverse (default 2).
            direction: "up", "down", or "both" (default).
            budget: Token budget for the whole pack. Symbols that do not fit are
                listed in `omitted` rather than dropped silently.
            per_symbol_cap: Max lines per symbol before explicit truncation.
            include_containment: Follow contains/method edges. **On by default
                here**, unlike blast_radius: measured on real fix commits,
                containment raised recall 0.351 -> 0.494 and improved precision,
                because co-changed symbols are frequently siblings or members
                (see AGENT-CONTEXT-COMPARISON.md §6).
            relations: Override the relation set entirely.
            index_budget: Tokens RESERVED (out of `budget`) for the index tier
                -- one file:line + signature line per symbol that did not fit
                as a body or sits one hop beyond `depth`; the index also gets
                what the bodies leave unspent. 0 disables it. Read `index` for
                what to open next and `related_tests` for what to run after
                the fix.
            graph: Path to graph.json.
        """
        try:
            from graphify_ext import context as ctxmod
            data, path = _load(graph)
            nid = _resolve(data, node, path)
            rels = tuple(relations) if relations else br.DEFAULT_RELATIONS
            if include_containment and not relations:
                rels = tuple(dict.fromkeys(rels + br.MEMBER_RELATIONS))
            manifest = None
            mp = path.parent / "manifest.json"
            if mp.exists():
                try:
                    m = graphio.read_json(mp)
                    manifest = m if isinstance(m, dict) else None
                except Exception:
                    manifest = None
            pack = ctxmod.build_context(
                data, nid, graphio.repo_root_for(path), depth=depth,
                direction=direction, budget=budget,
                per_symbol_cap=per_symbol_cap, relations=rels,
                manifest=manifest, index_budget=index_budget,
            )
            # A seed that could not be sliced is a partial result, not an "ok"
            # one: the agent asked for this symbol's code and did not get it.
            # Saying "0 symbols" would leave it unable to tell that from a
            # symbol that simply has no neighbours.
            status = "ok" if pack["seed_resolved"] else "partial"
            summary = (f"{len(pack['included'])} symbol(s) as source, "
                       f"{len(pack['index'])} indexed, "
                       f"{pack['tokens_used']}/{pack['budget']} tokens, "
                       f"{len(pack['unresolved'])} unresolved, "
                       f"{len(pack['omitted'])} omitted, "
                       f"{len(pack['unmodelled'])} absent from graph, "
                       f"{len(pack['related_tests'])} test link(s)")
            if pack["stale_files"]:
                status_note = (f"GRAPH STALE for {len(pack['stale_files'])} file(s) "
                               "shown -- line numbers may be wrong, re-extract; ")
                summary = status_note + summary
            if not pack["seed_resolved"]:
                summary = (f"SEED NOT SLICED ({pack['seed_unresolved_reason']}) — "
                           "no source for the requested symbol; " + summary)
            return {"status": status, "summary": summary, **pack}
        except Exception as exc:
            return _err(str(exc))

    @mcp.tool()
    def overrides_tool(node: str, graph: Optional[str] = None) -> dict:
        """Subclass implementations that override a method.

        A fix applied to a base method does not propagate to overrides; each
        one may need its own fix.

        Args:
            node: The (possibly inherited) method to check.
            graph: Path to graph.json.
        """
        try:
            data, path = _load(graph)
            nid = _resolve(data, node, path)
            overrides = br.overrides_of(data, nid)
            return {"status": "ok",
                    "summary": f"{len(overrides)} override(s) of {node}",
                    "overrides": overrides}
        except Exception as exc:
            return _err(str(exc))

    @mcp.tool()
    def triage_tool(
        vulns: list[dict],
        depth: int = 2,
        max_nodes: int = 300,
        relations: Optional[list[str]] = None,
        graph: Optional[str] = None,
    ) -> dict:
        """Full fix-context for each vulnerability in an identify-layer report.

        Per vuln: direct callers/callees, transitive blast radius, overrides,
        the taint-exposed subset, test coverage, and config dependencies.

        Args:
            vulns: Findings, each at minimum {"id","description","file","line"};
                an optional "function" overrides location-based resolution.
            depth: Blast-radius hops.
            max_nodes: Cap per radius.
            relations: Relations to traverse; omit for the default set.
            graph: Path to graph.json.
        """
        try:
            path = _graph_path(graph)
            if not path.exists():
                return _err(f"no graph at {path}")
            contexts = triage.triage_report(
                path, list(vulns), depth=depth, max_nodes=max_nodes,
                relations=tuple(relations) if relations else None)
            return {"status": "ok",
                    "summary": triage.summarize(contexts),
                    "contexts": contexts}
        except Exception as exc:
            return _err(str(exc))

    @mcp.tool()
    def edge_diff_tool(action: str, nodes: Optional[list[str]] = None,
                        out: Optional[str] = None) -> dict:
        """Pre/post-fix structural edge diff for target nodes.

        Call with action="snapshot" before editing, action="check" after
        re-extracting. A delta means the fix changed the node's structural
        relationships — intended (the vulnerable call removed) or not (a
        dropped validation call, a new sink).

        Args:
            action: "snapshot" or "check".
            nodes: Target nodes (required for snapshot).
            out: Output directory. Defaults to graphify-out.
        """
        try:
            out_dir = Path(out) if out else Path(DEFAULT_OUT)
            if action == "snapshot":
                if not nodes:
                    return _err("snapshot requires at least one node")
                entry = verify_fix.snapshot(out_dir, list(nodes))
                return {"status": "ok",
                        "summary": f"snapshot of {len(entry['nodes'])} node(s)",
                        "unresolved": entry["unresolved"], "nodes": entry["nodes"]}
            if action == "check":
                result = verify_fix.check(out_dir)
                return {"status": "ok",
                        "summary": ("no structural edge change" if result["clean"]
                                    else "EDGE DELTA — review before finalizing"),
                        **result}
            return _err(f"unknown action {action!r}; use snapshot or check")
        except Exception as exc:
            return _err(str(exc))

    @mcp.tool()
    def list_relations_tool(graph: Optional[str] = None) -> dict:
        """Relations present in this graph and whether each is followed by default.

        Use before narrowing a blast radius, so `relations` names something the
        graph actually contains.

        Args:
            graph: Path to graph.json.
        """
        try:
            import collections
            data, _ = _load(graph)
            counts = collections.Counter(
                str(e.get("relation", "")) for e in graphio.edges(data))
            default = set(br.DEFAULT_RELATIONS)
            rels = [{"relation": r, "count": n, "followed_by_default": r in default}
                    for r, n in counts.most_common()]
            return {"status": "ok",
                    "summary": f"{len(rels)} relation(s) in this graph",
                    "relations": rels}
        except Exception as exc:
            return _err(str(exc))

    @mcp.tool()
    def search_tool(query: str, limit: int = 25, graph: Optional[str] = None) -> dict:
        """Every node a name could mean, best match first.

        Use when a seed did not resolve, or before choosing one: returns id,
        label, qualified name (for supplement nodes such as `res.json`), file,
        line, whether it is callable, and which layer produced it.

        Args:
            query: Label, bare name, qualified name, id fragment or path fragment.
            limit: Max rows.
            graph: Path to graph.json.
        """
        try:
            data, _ = _load(graph)
            rows = graphio.candidates(data, query, limit=limit)
            return {"status": "ok",
                    "summary": f"{len(rows)} candidate(s) for {query!r}",
                    "candidates": rows}
        except Exception as exc:
            return _err(str(exc))

    @mcp.tool()
    def supplement_tool(dry_run: bool = False, graph: Optional[str] = None) -> dict:
        """Materialise definitions the extractor has no node for.

        Assignment-bound members (`res.json = function ...`) and definitions
        lost to id collisions (`@overload` stubs, `_get_x` vs `get_x`) get a
        real node, a contains/method edge from their owner, and conservative
        INFERRED `calls` edges. Existing nodes are never modified. Running it
        (not dry) also opts this output slot in to re-application after every
        rebuild. Idempotent.

        Args:
            dry_run: Report what would be added without writing.
            graph: Path to graph.json.
        """
        try:
            from graphify_ext import supplement
            path = _graph_path(graph)
            if not path.exists():
                return _err(f"no graph at {path}")
            root = graphio.repo_root_for(path)
            if dry_run:
                data = graphio.load(path)
                supplement.strip(data)
                manifest = None
                mp = path.parent / "manifest.json"
                if mp.exists():
                    try:
                        m = graphio.read_json(mp)
                        manifest = m if isinstance(m, dict) else None
                    except Exception:
                        manifest = None
                res = supplement.compute(data, root, manifest=manifest)
                return {"status": "ok",
                        "summary": (f"would add {len(res['nodes'])} node(s), "
                                    f"{len(res['edges'])} edge(s); "
                                    f"{len(res['stale_files'])} stale file(s) refused"),
                        "nodes": res["nodes"], "edges": res["edges"],
                        "skipped": res["skipped"], "stale_files": res["stale_files"],
                        "stats": res["stats"]}
            rep = supplement.apply(path, root=root)
            supplement.enable(path.parent)
            return {"status": "ok",
                    "summary": (f"added {rep['added_nodes']} node(s), "
                                f"{rep['added_edges']} edge(s); re-application "
                                "enabled for this slot"),
                    **rep}
        except Exception as exc:
            return _err(str(exc))

    @mcp.tool()
    def refresh_tool(paths: Optional[list[str]] = None, out: Optional[str] = None) -> dict:
        """Bring the graph up to date after editing, without leaving the tool.

        Runs graphify's incremental update for `paths` (default: every file
        whose manifest hash no longer matches the working tree), then
        re-applies the supplement (if enabled) and injected edges. Call it
        after an edit and before re-reading context; `stale_files` in a
        context result is the signal. Never performs a full rebuild.

        Args:
            paths: Repo-relative files that changed. Omit to detect them.
            out: Output directory. Defaults to graphify-out.
        """
        try:
            from graphify_ext import refresh as rf
            rep = rf.refresh(Path(out) if out else Path(DEFAULT_OUT),
                             paths=list(paths) if paths else None)
            return {"status": "ok" if rep.get("ok") else "error",
                    "summary": rf.format_report(rep).splitlines()[0], **rep}
        except Exception as exc:
            return _err(str(exc))

    return mcp


def apply_tool_filter(mcp, tools: Optional[str] = None) -> None:
    """Restrict the server to an allow-list. Copied from crg main.py:1114-1173.

    Removal goes through ``mcp.local_provider.remove_tool``; the private
    ``_tool_manager._tools`` path was removed in fastmcp>=3.0.
    """
    import asyncio

    raw = tools or os.environ.get("GRAPHIFY_EXT_TOOLS")
    if not raw:
        return
    allowed = {t.strip() for t in raw.split(",") if t.strip()}
    if not allowed:
        return

    def _list_tool_names() -> list[str]:
        # list_tools is async; main() calls this before the loop starts, but a
        # test may call it from inside one — then run it on a worker loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return [t.name for t in asyncio.run(mcp.list_tools())]
        import concurrent.futures

        def _runner() -> list[str]:
            return [t.name for t in asyncio.run(mcp.list_tools())]

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_runner).result()

    for name in _list_tool_names():
        if name not in allowed:
            mcp.local_provider.remove_tool(name)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="graphify-ext-mcp",
        description="MCP server exposing graphify-ext fix-context tools")
    ap.add_argument("--tools", default=None,
                    help="comma-separated allow-list (env: GRAPHIFY_EXT_TOOLS). "
                         f"Available: {', '.join(TOOL_NAMES)}")
    ap.add_argument("--http", action="store_true",
                    help="serve over streamable-http instead of stdio")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--list-tools", action="store_true",
                    help="print the tool names this server exposes and exit")
    args = ap.parse_args(argv)

    if args.list_tools:
        for name in TOOL_NAMES:
            print(name)
        return 0

    try:
        mcp = build_server()
    except ImportError:
        print("error: fastmcp is not installed in this interpreter.\n"
              "       graphify-ext keeps it optional so the CLI and git hooks\n"
              "       stay dependency-free. Install with:  pip install -e \".[mcp]\"",
              file=sys.stderr)
        return 1

    apply_tool_filter(mcp, args.tools)

    if args.http:
        host = args.host or "127.0.0.1"
        port = args.port or 5599
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        # Stdio must keep stdout strictly JSON-RPC — the banner corrupts the
        # handshake for clients like Codex CLI (crg main.py:1236-1237).
        mcp.run(transport="stdio", show_banner=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
